"""Statistics of your games, from what the stages stored: one self-contained HTML page (inline SVG, no libraries).

`collect` reads the cache (your games, the annotate stage's grades, candidates, the notes the add-on published)
into plain data; `render` draws it. The add-on shows the page in a window and saves it as a file; the command
line can write it too.

The grades are those of the annotated PGN export (export.grades_of): ?!, ?, ?? from the win rate a move lost.
Rates are per 100 of your annotated moves, so games that are only partly annotated count for what is.
"""

from __future__ import annotations

import html
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import export, openings, phases
from .config import Config
from .select import BLUNDER, DUBIOUS, MISTAKE
from .stages import extract

MONTHS = 24  # the months shown, the latest ones
OPENINGS = 15  # the openings listed, the most played ones
PHASES = (phases.OPENING, phases.MIDDLEGAME, phases.ENDGAME)
TIME_CLASSES = ("ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence")


@dataclass
class Tally:
    games: int = 0
    points: float = 0.0  # your result, summed (win 1, draw ½)
    moves: int = 0  # your annotated moves
    dubious: int = 0
    mistakes: int = 0
    blunders: int = 0

    def add(self, nag: int) -> None:
        self.moves += 1
        if nag == DUBIOUS:
            self.dubious += 1
        elif nag == MISTAKE:
            self.mistakes += 1
        elif nag == BLUNDER:
            self.blunders += 1

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Stats:
    games: int = 0
    your_moves: int = 0  # every move you played (annotated or not)
    total: Tally = field(default_factory=Tally)
    months: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    phases: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    time_classes: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    openings: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    colors: dict[str, list[int]] = field(default_factory=lambda: {"white": [0, 0, 0], "black": [0, 0, 0]})
    notes: int | None = None  # puzzle notes in Anki (None: the add-on hasn't published them)
    waiting: int = 0  # candidates waiting for the build stage
    made: int = 0  # candidates that made a puzzle

    def to_json(self) -> dict[str, Any]:
        def tallies(d: dict[str, Tally]) -> dict[str, Any]:
            return {k: t.to_json() for k, t in d.items()}

        return {
            "games": self.games,
            "your_moves": self.your_moves,
            "total": self.total.to_json(),
            "months": tallies(self.months),
            "phases": tallies(self.phases),
            "time_classes": tallies(self.time_classes),
            "openings": tallies(self.openings),
            "colors": self.colors,
            "notes": self.notes,
            "waiting": self.waiting,
            "made": self.made,
        }


def collect(cfg: Config, cache) -> Stats:
    """The statistics of every stored game of your accounts (whatever the look-back and filters)."""
    st = Stats()
    have = cache.annotations()
    for g in extract.games_of(cfg, cache, everything=True):
        st.games += 1
        mine = 0 if g.user_is_white else 1
        st.your_moves += sum(1 for ply in range(len(g.moves)) if ply % 2 == mine)
        result = g.user_result()
        st.colors["white" if g.user_is_white else "black"][0 if result == 1 else 1 if result == 0.5 else 2] += 1
        month = time.strftime("%Y-%m", time.localtime(g.end_ts)) if g.end_ts else "?"
        _, name = openings.classify(g.moves)
        groups = [
            st.total,
            st.months[month],
            st.time_classes[g.time_class or "other"],
            st.openings[openings.family(name)],
        ]
        for t in groups:
            t.games += 1
            t.points += result
        division = phases.divide(g.moves)
        seen_phases: set[str] = set()
        for ply, gr in export.grades_of(g, g.user_is_white, have, cfg.annotate.inaccuracy).items():
            if not gr.mine:
                continue
            ph = phases.phase_at(division, ply)
            if ph not in seen_phases:
                seen_phases.add(ph)
                st.phases[ph].games += 1
            for t in (*groups, st.phases[ph]):
                t.add(gr.nag)
    published = cache.published_notes()
    st.notes = None if published is None else sum(1 for k in published if not k.startswith("legacy:"))
    st.waiting = len(cache.candidates())
    st.made = len(cache.candidates("made"))
    return st


# --- the page

GRADES = (("dubious", "?!", "Inaccuracies"), ("mistakes", "?", "Mistakes"), ("blunders", "??", "Blunders"))
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _per100(t: Tally, key: str) -> float:
    return 100 * getattr(t, key) / t.moves if t.moves else 0.0


def _month_label(m: str, long: bool = False) -> str:
    try:
        y, mo = m.split("-")
        return f"{MONTH_NAMES[int(mo) - 1]} {y if long else y[2:]}"
    except (ValueError, IndexError):
        return m


def _nice(v: float) -> float:
    """A round axis top at or above `v`."""
    for mag in (1, 10, 100, 1000):
        for step in (1, 2, 2.5, 5):
            if step * mag >= v:
                return step * mag
    return v


def _tip(label: str, t: Tally) -> str:
    rates = " · ".join(f"{sym} {_per100(t, key):.1f}" for key, sym, _ in reversed(GRADES))
    games = f"{t.games} game{'s' if t.games != 1 else ''}"
    return f"{label}\n{games} · {t.moves} moves annotated\nPer 100 moves: {rates}"


def _bars(rows: list[tuple[str, str, Tally]], label_every: int = 1) -> str:
    """Stacked bars of ?!, ? and ?? per 100 moves, one per row (key, label, tally); a row without annotated
    moves gets no bar."""
    w, h, left, top, bottom = 640, 220, 36, 8, 28
    plot_h = h - top - bottom
    n = max(1, len(rows))
    band = (w - left) / n
    bar = min(48.0, band * 0.6)
    peak = max((sum(_per100(t, k) for k, _, _ in GRADES) for _, _, t in rows), default=0.0)
    ymax = _nice(max(peak, 1.0))
    y = lambda v: top + plot_h * (1 - v / ymax)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" class="chart">']
    for i in range(5):
        v = ymax * i / 4
        out.append(f'<line class="grid" x1="{left}" x2="{w}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="tick" x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{w}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    for i, (_key, label, t) in enumerate(rows):
        cx = left + band * (i + 0.5)
        x0 = cx - bar / 2
        base = 0.0
        parts = [(k, _per100(t, k)) for k, _, _ in GRADES if _per100(t, k) > 0]
        for j, (k, v) in enumerate(parts):
            y1, y0 = y(base + v), y(base)
            gap = 2 if j else 0  # a surface gap between stacked segments
            hgt = max(0.0, y0 - y1 - gap)
            if j == len(parts) - 1 and hgt > 4:  # the data end, rounded
                r = 4
                out.append(
                    f'<path class="g-{k}" d="M{x0:.1f},{y0 - gap:.1f} V{y1 + r:.1f} Q{x0:.1f},{y1:.1f} {x0 + r:.1f},{y1:.1f}'
                    f' H{x0 + bar - r:.1f} Q{x0 + bar:.1f},{y1:.1f} {x0 + bar:.1f},{y1 + r:.1f} V{y0 - gap:.1f} Z"/>'
                )
            else:
                out.append(f'<rect class="g-{k}" x="{x0:.1f}" y="{y1:.1f}" width="{bar:.1f}" height="{hgt:.1f}"/>')
            base += v
        if i % label_every == 0 or i == len(rows) - 1:
            out.append(f'<text class="tick" x="{cx:.1f}" y="{h - 8}" text-anchor="middle">{html.escape(label)}</text>')
        out.append(
            f'<rect class="hit" x="{cx - band / 2:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}"'
            f' data-tip="{html.escape(_tip(label, t))}"/>'
        )
    out.append("</svg>")
    return "".join(out)


def _table(rows: list[tuple[str, str, Tally]], first: str) -> str:
    head = "".join(f"<th>{sym} per 100</th>" for _, sym, _ in GRADES)
    body = "".join(
        f"<tr><td>{html.escape(label)}</td><td>{t.games}</td><td>{t.moves}</td>"
        + "".join(f"<td>{_per100(t, k):.1f}</td>" for k, _, _ in GRADES)
        + "</tr>"
        for _, label, t in rows
    )
    return (
        f"<details><summary>Show as a table</summary><table><thead><tr><th>{first}</th><th>Games</th>"
        f"<th>Moves annotated</th>{head}</tr></thead><tbody>{body}</tbody></table></details>"
    )


def _chart(title: str, sub: str, rows: list[tuple[str, str, Tally]], first: str, label_every: int = 1) -> str:
    if not any(t.moves for _, _, t in rows):
        return f'<section><h2>{title}</h2><p class="sub">No annotated moves yet.</p></section>'
    return (
        f'<section><h2>{title}</h2><p class="sub">{sub}</p>{_bars(rows, label_every)}'
        f"{_table([(k, l, t) for k, l, t in rows if t.games], first)}</section>"
    )


def _tile(value: str, label: str, note: str = "") -> str:
    note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
    return f'<div class="tile"><div class="value">{value}</div><div class="label">{label}</div>{note_html}</div>'


def _score(t: Tally) -> str:
    return f"{100 * t.points / t.games:.0f}%" if t.games else "–"


CSS = """
:root { color-scheme: light;
  --surface: #ffffff; --plane: #f9f9f9; --ink: #191919; --ink-2: #666666; --grid: #e7e7e7; --axis: #c2c2c2;
  --border: rgba(11,11,11,0.10); --g-dubious: #84b9f3; --g-mistakes: #257de0; --g-blunders: #0f4a96; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark;
  --surface: #191919; --plane: #0b0b0b; --ink: #efefef; --ink-2: #9c9c9c; --grid: #303030; --axis: #474747;
  --border: rgba(255,255,255,0.10); --g-dubious: #1757ac; --g-mistakes: #338ced; --g-blunders: #9dc8f7; } }
:root[data-theme="dark"] { color-scheme: dark;
  --surface: #191919; --plane: #0b0b0b; --ink: #efefef; --ink-2: #9c9c9c; --grid: #303030; --axis: #474747;
  --border: rgba(255,255,255,0.10); --g-dubious: #1757ac; --g-mistakes: #338ced; --g-blunders: #9dc8f7; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--plane); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 760px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 2px; } h2 { font-size: 15px; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0 0 10px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; margin: 14px 0; }
.tile, section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
section { margin: 0 0 12px; }
.value { font-size: 24px; font-weight: 600; } .label { color: var(--ink-2); } .note { color: var(--ink-2); font-size: 12px; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; color: var(--ink-2); margin: 0 0 12px; }
.legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 6px; vertical-align: -1px; background: var(--sw); }
.chart { width: 100%; height: auto; display: block; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; } .axis { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--ink-2); font-size: 11px; font-variant-numeric: tabular-nums; }
.g-dubious { fill: var(--g-dubious); } .g-mistakes { fill: var(--g-mistakes); } .g-blunders { fill: var(--g-blunders); }
.hit { fill: transparent; } .hit:hover { fill: var(--ink); fill-opacity: 0.04; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; margin-top: 8px; }
th, td { text-align: right; padding: 4px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; white-space: normal; } th { color: var(--ink-2); font-weight: 500; }
details summary { color: var(--ink-2); cursor: pointer; margin-top: 6px; }
.scroll { overflow-x: auto; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); white-space: pre;
  border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; font-size: 12px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
footer { color: var(--ink-2); font-size: 12px; margin: 8px 0 24px; }
"""

JS = """
const tip = document.getElementById('tip');
document.addEventListener('mousemove', e => {
  const t = e.target.closest && e.target.closest('[data-tip]');
  if (!t) { tip.hidden = true; return; }
  tip.textContent = t.dataset.tip; tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 8) + 'px';
  tip.style.top = Math.min(e.clientY + 14, innerHeight - r.height - 8) + 'px';
});
"""


def render(st: Stats, theme: str | None = None, generated: float | None = None) -> str:
    """The page. `theme`: "light" or "dark" to force one (the add-on follows Anki's); None follows the system."""
    t = st.total
    share = f"{100 * t.moves / st.your_moves:.0f}% of your moves" if st.your_moves else ""
    errors = (_per100(t, "mistakes") + _per100(t, "blunders")) if t.moves else None
    tiles = "".join(
        [
            _tile(f"{st.games:,}", "games", f"you scored {_score(t)}" if st.games else ""),
            _tile(f"{t.moves:,}", "moves annotated", share),
            _tile(f"{errors:.1f}" if errors is not None else "–", "? and ?? per 100 moves"),
            _tile(f"{st.notes:,}" if st.notes is not None else "–", "puzzle notes in Anki"),
            _tile(
                f"{st.waiting:,}",
                "candidates waiting",
                f"{st.made:,} made a puzzle" if st.made == 1 else f"{st.made:,} made puzzles" if st.made else "",
            ),
        ]
    )
    legend = "".join(f'<span style="--sw: var(--g-{k})">{sym} {name}</span>' for k, sym, name in GRADES)
    months = sorted(m for m in st.months if m != "?")[-MONTHS:]
    month_rows = [(m, _month_label(m), st.months[m]) for m in months]
    phase_rows = [(p, p.capitalize(), st.phases[p]) for p in PHASES if p in st.phases]
    tcs = sorted(st.time_classes, key=lambda c: TIME_CLASSES.index(c) if c in TIME_CLASSES else 99)
    tc_rows = [(c, c.capitalize(), st.time_classes[c]) for c in tcs]
    sections = [
        _chart(
            "By month",
            "Your inaccuracies (?!), mistakes (?) and blunders (??) per 100 annotated moves.",
            month_rows,
            "Month",
            label_every=max(1, len(month_rows) // 12),
        ),
        _chart("By phase of the game", "Where in the game they happen.", phase_rows, "Phase"),
        _chart("By time control", "How the clock changes them.", tc_rows, "Time control"),
    ]
    ops = sorted(((n, o) for n, o in st.openings.items() if o.games), key=lambda x: (-x[1].games, x[0]))[:OPENINGS]
    op_rows = "".join(
        f"<tr><td>{html.escape(n)}</td><td>{o.games}</td><td>{_score(o)}</td><td>{o.moves}</td>"
        f"<td>{(_per100(o, 'mistakes') + _per100(o, 'blunders')):.1f}</td></tr>"
        if o.moves
        else f"<tr><td>{html.escape(n)}</td><td>{o.games}</td><td>{_score(o)}</td><td>0</td><td>–</td></tr>"
        for n, o in ops
    )
    if ops:
        sections.append(
            "<section><h2>Openings</h2><p class='sub'>Your most played, with your score and your ? and ?? per 100"
            " annotated moves in those games.</p><div class='scroll'><table><thead><tr><th>Opening</th><th>Games</th>"
            f"<th>Score</th><th>Moves annotated</th><th>? + ?? per 100</th></tr></thead><tbody>{op_rows}</tbody>"
            "</table></div></section>"
        )
    color_rows = "".join(
        f"<tr><td>{c.capitalize()}</td><td>{sum(wdl)}</td><td>{wdl[0]}</td><td>{wdl[1]}</td><td>{wdl[2]}</td>"
        f"<td>{(100 * (wdl[0] + wdl[1] / 2) / sum(wdl)):.0f}%</td></tr>"
        for c, wdl in st.colors.items()
        if sum(wdl)
    )
    if color_rows:
        sections.append(
            "<section><h2>Results by colour</h2><table><thead><tr><th>You played</th><th>Games</th><th>Won</th>"
            f"<th>Drawn</th><th>Lost</th><th>Score</th></tr></thead><tbody>{color_rows}</tbody></table></section>"
        )
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated if generated is not None else time.time()))
    theme_attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    data = json.dumps(st.to_json()).replace("</", "<\\/")  # the numbers, for other programs
    return (
        f'<!doctype html><html lang="en"{theme_attr}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Knightly Review statistics</title>'
        f"<style>{CSS}</style></head><body><main><h1>Your games</h1>"
        '<p class="sub">Every stored game of your accounts, as the engine judged your moves.</p>'
        f'<div class="tiles">{tiles}</div><div class="legend">{legend}</div>{"".join(sections)}'
        f"<footer>Knightly Review · {when}. Moves are annotated ply by ply, so recent games may be partly "
        "annotated: rates count annotated moves only.</footer></main>"
        f'<div id="tip" hidden></div><script type="application/json" id="data">{data}</script>'
        f"<script>{JS}</script></body></html>"
    )

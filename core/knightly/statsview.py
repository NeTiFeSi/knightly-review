"""The stats of a position as the Notes field shows them: an opening-explorer style table.

For a position reached in at least two distinct games: how many games reached it and how they ended (White /
draw / Black, as Lichess's explorer shows), then a row per move played from it (the six most played, the rest
in one row) with its number of games, its share, and the same bar. Moves the note's analysis graded carry
their NAG and a green or red accent. Plain HTML with classes (the styles are in the note type's CSS) and inline
widths, and every row reads as a sentence without any style at all. The card script shows only the section that
belongs to its card (`data-card`).
"""

from __future__ import annotations

import html
from typing import Optional

import chess

from .select import ACCEPTED, BLUNDER, DUBIOUS, MISTAKE, NAG_SYMBOL
from .stats import MoveResults, Position

MIN_GAMES = 2  # a position needs this many distinct games to have stats
ROWS = 6  # moves listed one by one; the rest are one row
LABEL_MIN = 8  # a bar segment narrower than this (in %) has no label


def percents(parts: list[int]) -> list[int]:
    """`parts` as whole percentages of their sum that add up to 100 (largest remainder); zeros for no total."""
    total = sum(parts)
    if not total:
        return [0] * len(parts)
    raw = [100 * p / total for p in parts]
    out = [int(r) for r in raw]
    for i in sorted(range(len(parts)), key=lambda i: raw[i] - out[i], reverse=True)[: 100 - sum(out)]:
        out[i] += 1
    return out


def bar(r: MoveResults, cls: str) -> str:
    """The White / draw / Black bar of `r` (empty when no game of it has a result)."""
    if not r.decided:
        return ""
    w, d, b = percents([r.white, r.draw, r.black])
    parts = []
    for c, pct in (("w", w), ("d", d), ("b", b)):
        if pct:
            parts.append(f'<i class="{c}" style="width:{pct}%">{pct}%</i>' if pct >= LABEL_MIN
                         else f'<i class="{c}" style="width:{pct}%"></i>')
    title = f"White {r.white} · draws {r.draw} · Black {r.black}"
    return f'<span class="{cls}" title="{title}">{"".join(parts)}</span>'


def _games(n: int) -> str:
    return f"{n} game" + ("" if n == 1 else "s")


def _results_text(r: MoveResults) -> str:
    if not r.decided:
        return ""
    w, d, b = percents([r.white, r.draw, r.black])
    return f"White {r.white} ({w}%) · draws {r.draw} ({d}%) · Black {r.black} ({b}%)"


def section(card: int, pos: Optional[Position], heading: str = "", nags: Optional[dict[str, int]] = None,
            mistake: str = "") -> str:
    """The stats of `pos` for the card numbered `card`, or '' when fewer than `MIN_GAMES` games reached it.
    `nags`: UCI → NAG of the moves the note graded; `mistake`: the UCI of the note's mistake (red)."""
    if pos is None or pos.reached < MIN_GAMES:
        return ""
    nags = nags or {}
    board = chess.Board(pos.fen)
    moves = sorted(pos.moves.items(), key=lambda kv: -kv[1].games)
    total = pos.totals()
    head = f"{heading}{_games(pos.reached)} reached this position"
    if total.decided:
        head += " · " + _results_text(total)
    out = [f'<div class="kr-stats" data-card="{card}">', f'<div class="kr-stats-head">{html.escape(head)}</div>']
    if total.decided:
        out.append(bar(total, "kr-stats-bar"))
    for u, r in moves[:ROWS]:
        try:
            san = board.san(chess.Move.from_uci(u))
        except ValueError:
            continue
        nag = nags.get(u, 0)
        tint = (" kr-bad" if u == mistake or nag in (MISTAKE, BLUNDER, DUBIOUS) else
                " kr-good" if nag in ACCEPTED else "")
        glyph = f'<b class="kr-stat-nag">{html.escape(NAG_SYMBOL[nag])}</b>' if nag in NAG_SYMBOL else ""
        share = round(100 * r.games / pos.reached)
        out.append(f'<div class="kr-stat-row{tint}"><span class="kr-stat-move">{html.escape(san)}{glyph}</span>'
                   f'<span class="kr-stat-games">{_games(r.games)} · {share}%</span>{bar(r, "kr-stat-wdl")}</div>')
    rest = moves[ROWS:]
    if rest:
        other = MoveResults(games=sum(r.games for _, r in rest), white=sum(r.white for _, r in rest),
                            draw=sum(r.draw for _, r in rest), black=sum(r.black for _, r in rest))
        n = len(rest)
        out.append(f'<div class="kr-stat-row kr-stat-other"><span class="kr-stat-move">other {n} move'
                   f'{"" if n == 1 else "s"}</span><span class="kr-stat-games">{_games(other.games)} · '
                   f'{round(100 * other.games / pos.reached)}%</span>{bar(other, "kr-stat-wdl")}</div>')
    out.append("</div>")
    return "".join(out)

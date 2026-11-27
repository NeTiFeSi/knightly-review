"""The stages side by side: one worker thread each, chained, sharing the engine pool, the cache and an outbox.

Each stage reads its input tables and writes its output tables (see `cache.py`), so it can run whenever its input
changed, alongside the others:

    extract ──▶ annotate ──▶ select ──▶ build ──▶ outbox ──▶ the collection (the add-on writes it)
       └──────▶ manage ───────────────────────────▶ outbox

- extract runs at start (`update_on_startup`), every `update_every_hours`, and by hand; when it ends, manage and
  annotate run;
- annotate scans, ply by ply, every position of your games (and your opponents', with `include_opponent`) that
  has no annotation yet, for `annotate_minutes` per run; with `keep_annotating` it goes on at low priority
  until everything is annotated or `keep_annotating_hours` are up. Whenever it annotated something, select runs;
- select turns each fully annotated ply into candidates; when there are candidates, build runs;
- build makes up to `max_new_cards` cards per run, from the candidates in order, in `analysis_minutes`;
- manage refreshes and rebuilds the notes already in the collection, and lists the out-of-date ones.

Notes go to the outbox table; whoever owns the collection drains it (`Supervisor.snapshot` counts the notes
waiting there as in the collection). The engine is the shared pool (engine/broker.py): build's searches come
before annotate's. Build and manage both write notes, so they never run at the same time.

`run_all` runs every stage once, one after another (the command line, tests).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

from . import http, puzzles, stats
from .cache import Cache
from .config import Config
from .engine.analyse import Analyser
from .engine.broker import BACKFILL, BUILD, CONFIRM, SCAN, Pool
from .jobs import collection_state
from .model import NoteSpec
from .progress import Tracker
from .stages import extract
from .stages.annotate import Annotator, annotated, positions, scan_from_json, scan_to_json
from .stages.budget import UNBOUNDED, Budget, Stop
from .stages.build import Builder
from .stages.candidates import Ledger, Selector
from .stages.manage import Manager
from .stages.report import Reporter

log = logging.getLogger(__name__)

STAGES = ("extract", "manage", "annotate", "select", "build")
NEXT = {"extract": ("manage", "annotate"), "annotate": ("select",), "select": ("build",)}
NOTE, SUSPEND = "note", "suspend"
IDLE_ENGINE_SECONDS = 60


@dataclass
class Outcome:
    progressed: bool = False  # wrote something the next stages can use
    more: bool = False  # work left that this run's budget didn't cover
    summary: str = ""
    notes: list[NoteSpec] = field(default_factory=list)  # build, manage: the notes put in the outbox
    warnings: list[str] = field(default_factory=list)


@dataclass
class Context:
    """What every stage run gets."""

    cfg: Config
    cache: Cache
    pool: Pool
    cancel: threading.Event
    existing: Callable[[], dict[str, str] | None]  # the collection's notes, key → PGN (None: no collection)
    notes_lock: threading.Lock = field(default_factory=threading.Lock)
    background: bool = False  # annotate: a low-priority pass after the first
    analyser_factory: Callable[..., Analyser] | None = None  # tests: (cfg, cache, cancel) → an analyser

    def analyser(self, tracker: Tracker, priority: int, scan_priority: int):
        if self.analyser_factory is not None:
            return self.analyser_factory(self.cfg, self.cache, self.cancel)
        return Analyser(
            self.cfg, self.cache, self.cancel, tracker, pool=self.pool, priority=priority, scan_priority=scan_priority
        )

    def snapshot(self) -> tuple[dict[str, str], bool]:
        """The notes on cards: the collection's, and those waiting in the outbox (newest version).

        The outbox is read first: a note drained between the two reads is then in the collection's notes, never
        in neither (a note missing from both would look deleted, and manage would drop its puzzle record)."""
        waiting = self.cache.outbox()
        got = self.existing()
        notes = dict(got or {})
        for _seq, kind, data in waiting:
            if kind == NOTE:
                notes[data["key"]] = data["pgn"]
        return notes, got is not None


def _sides(cfg: Config) -> tuple[bool, ...]:
    return (True, False) if cfg.select.include_opponent else (True,)


def _games(ctx: Context):
    games = extract.games_of(ctx.cfg, ctx.cache)
    return games, stats.collect(games)


# --- the stages, one bounded run each


def run_extract(ctx: Context, tracker: Tracker, manual: bool = False) -> Outcome:
    tracker.stage("fetch", "Fetching games…")
    got = extract.run(
        ctx.cfg, ctx.cache, force=manual, cancel=ctx.cancel, message=lambda m: tracker.message(m, force=True)
    )
    n = got.new_games
    return Outcome(True, summary=f"{n} new game{'s' if n != 1 else ''}", warnings=got.warnings)


def run_annotate(ctx: Context, tracker: Tracker, manual: bool = False) -> Outcome:
    cfg, cache = ctx.cfg, ctx.cache
    _, by_ply = _games(ctx)
    depth = cfg.annotate.scan_depth_used()
    have = cache.annotations()
    todo = [
        (ply, u)
        for ply in sorted(by_ply)
        for mine in _sides(cfg)
        for u in positions(by_ply, ply, mine)
        if not annotated(have.get((u.pos.key, ply, mine)), u, depth)
    ]
    if not todo:
        return Outcome(summary="every position is annotated")
    prio = BACKFILL if ctx.background else SCAN
    budget = Budget(cfg.annotate.annotate_minutes, UNBOUNDED)
    tracker.stage("analyse", f"Annotating {len(todo)} positions at depth {depth}", total=len(todo), unit="positions")
    done = 0
    with ctx.analyser(tracker, prio, prio) as an:
        annotator = Annotator(an, cfg, tracker)
        try:
            for ply, u in todo:
                budget.tick()
                annotator.scan([u], ply, lambda: None)
                cache.put_annotation(u.pos.key, ply, u.mine, depth, scan_to_json(u.scan))
                done += 1
        except Stop:
            pass
    left = len(todo) - done
    return Outcome(done > 0, left > 0, f"{done} positions annotated" + (f", {left} to go" if left else ""))


def _units_by_key(by_ply, ply: int, mine: bool):
    return {u.pos.key: u for u in positions(by_ply, ply, mine)}


def run_select(ctx: Context, tracker: Tracker, manual: bool = False) -> Outcome:
    cfg, cache = ctx.cfg, ctx.cache
    fp = cfg.fingerprint("annotate", "select")
    if cache.get("candidates_settings") != fp:  # what counts as a candidate changed: judge them all again
        cache.clear_candidates()
        cache.set("candidates_settings", fp)
    existing, have_collection = ctx.snapshot()
    if not have_collection:  # what is on cards is unknown: no candidate can be told apart from them
        return Outcome(summary="no collection")
    _store, known, claimed = collection_state(cache, existing, prune=False)
    selector = Selector(cfg, Ledger.of(known, claimed))
    _, by_ply = _games(ctx)
    depth = cfg.annotate.scan_depth_used()
    have = cache.annotations()
    rows = []
    complete = -1
    for ply in sorted(by_ply):
        ready = True
        for mine in _sides(cfg):
            units = selector.open(positions(by_ply, ply, mine), ply)
            if not all(annotated(have.get((u.pos.key, ply, mine)), u, depth) for u in units):
                ready = False
                break
            for u in units:
                u.scan = scan_from_json(have[(u.pos.key, ply, mine)][1])
            rows += [(u.pos.key, ply, mine, u.loss(), selector.tries(u, ply)) for u in selector.rank(units, ply)]
        if not ready:
            break  # a ply is ranked only once all of it is annotated
        complete = ply
    added = cache.add_candidates(rows)
    pending = len(cache.candidates())
    tracker.message(f"Candidates: {added} new, {pending} waiting", force=True)
    return Outcome(pending > 0, summary=f"{added} new candidates, up to ply {complete}; {pending} waiting")


def run_build(ctx: Context, tracker: Tracker, manual: bool = False) -> Outcome:
    cfg, cache = ctx.cfg, ctx.cache
    pending = cache.candidates()
    if not pending:
        return Outcome(summary="no candidates")
    with ctx.notes_lock:
        existing, have_collection = ctx.snapshot()
        if not have_collection:
            return Outcome(summary="no collection")
        store, known, claimed = collection_state(cache, existing, prune=False)
        ledger = Ledger.of(known, claimed)
        selector = Selector(cfg, ledger)
        games, by_ply = _games(ctx)
        have = cache.annotations()
        budget = Budget(cfg.build.analysis_minutes, cfg.build.max_new_cards)
        out = Outcome()
        with ctx.analyser(tracker, CONFIRM, BUILD) as an:
            builder: Builder

            def on_note(spec: NoteSpec) -> None:
                cache.outbox_put(NOTE, spec.to_json())
                out.notes.append(spec)

            report = Reporter(tracker, budget, True, lambda: builder.cards)
            builder = Builder(
                games,
                an,
                cfg,
                store,
                ledger,
                Annotator(an, cfg, tracker),
                budget,
                report,
                tracker,
                existing=existing,
                on_note=on_note,
            )
            report.stage("Making puzzles", len(pending), "candidates", f"depth {cfg.annotate.sf_depth}, every move")
            units: dict[tuple[int, bool], dict] = {}
            try:
                for key, ply, mine, _loss, tries in pending:
                    budget.tick()
                    by_key = units.setdefault((ply, mine), _units_by_key(by_ply, ply, mine))
                    u = by_key.get(key)
                    row = have.get((key, ply, mine))
                    if u is None or row is None or not selector.open([u], ply):
                        cache.set_candidate(key, ply, mine, "skipped")  # gone with its games, or on a card now
                        continue
                    u.scan = scan_from_json(row[1])
                    before = builder.found
                    builder.confirm(u, ply, [(m, loss) for m, loss in tries])
                    cache.set_candidate(key, ply, mine, "made" if builder.found > before else "rejected")
                    if builder.cards >= budget.want:
                        raise Stop()
            except Stop:  # an unfinished candidate stays pending: the next run makes the notes it is missing
                pass
        left = len(cache.candidates())
        out.progressed, out.more = bool(out.notes), left > 0
        out.summary = f"{builder.cards} new card{'s' if builder.cards != 1 else ''}; {left} candidates waiting"
        return out


def run_manage(ctx: Context, tracker: Tracker, manual: bool = False) -> Outcome:
    cfg, cache = ctx.cfg, ctx.cache
    with ctx.notes_lock:
        existing, have_collection = ctx.snapshot()
        if not have_collection:
            return Outcome(summary="no collection")
        games, _ = _games(ctx)
        fp = _manage_fingerprint(cfg, existing, games)
        if not manual and cache.get("manage_last") == fp:
            return Outcome(summary="nothing changed")
        store, known, claimed = collection_state(cache, existing, prune=True)
        ledger = Ledger.of(known, claimed)
        budget = Budget(cfg.build.analysis_minutes, cfg.build.max_new_cards)
        out = Outcome()
        with ctx.analyser(tracker, BUILD, BUILD) as an:
            builder: Builder

            def on_note(spec: NoteSpec) -> None:
                cache.outbox_put(NOTE, spec.to_json())
                out.notes.append(spec)

            report = Reporter(tracker, budget, True, lambda: builder.cards)
            builder = Builder(
                games,
                an,
                cfg,
                store,
                ledger,
                Annotator(an, cfg, tracker),
                budget,
                report,
                tracker,
                existing=existing,
                on_note=on_note,
            )
            manager = Manager(builder, cfg, budget, report, tracker, extract.stored_urls(cfg, cache))
            finished = True
            try:
                manager.run()
            except Stop:
                finished = False
            outdated = manager.outdated()
        if cfg.manage.suspend_outdated and outdated:
            cache.outbox_put(SUSPEND, outdated)
        if finished and not out.notes and not builder.unconfirmed:
            cache.set("manage_last", fp)
        out.progressed, out.more = bool(out.notes), not finished
        out.summary = f"{len(out.notes)} notes updated, {len(outdated)} out of date"
        return out


def _manage_fingerprint(cfg: Config, existing: dict[str, str], games) -> str:
    blob = json.dumps(
        [
            puzzles.RULES,
            cfg.fingerprint("manage", "annotate", "select"),
            sorted(existing.items()),
            sorted(g.key for g in games),
        ]
    )
    return hashlib.sha256(blob.encode()).hexdigest()


WORKERS: dict[str, Callable[[Context, Tracker, bool], Outcome]] = {
    "extract": run_extract,
    "manage": run_manage,
    "annotate": run_annotate,
    "select": run_select,
    "build": run_build,
}


# --- the supervisor


@dataclass
class StageState:
    name: str
    tracker: Tracker = field(default_factory=Tracker)
    running: bool = False
    last: Outcome | None = None
    error: str = ""
    finished_at: float = 0.0


class Supervisor:
    def __init__(
        self,
        ctx: Context,
        *,
        on_done: Callable[[str, Outcome | None, str], None] | None = None,
        on_start: Callable[[str, Tracker], None] | None = None,
        workers: dict[str, Callable[[Context, Tracker, bool], Outcome]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`on_start(stage, tracker)`: as a stage run starts, with the tracker it reports to; `on_done(stage,
        outcome, error)`: after it (both on that stage's thread)."""
        self.ctx = ctx
        self.on_done = on_done or (lambda *a: None)
        self.on_start = on_start or (lambda *a: None)
        self.workers = dict(workers or WORKERS)
        self.clock = clock
        self.state = {name: StageState(name) for name in STAGES}
        self._wake = {name: threading.Event() for name in STAGES}
        self._manual = dict.fromkeys(STAGES, False)
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self.keep_until = 0.0  # keep annotating until this time (monotonic); 0 = not keeping on

    # --- running one stage
    def run_stage(self, name: str, manual: bool = False) -> Outcome | None:
        """Run one stage now, on this thread. Returns None when it failed or was cancelled."""
        st = self.state[name]
        st.running, st.error = True, ""
        st.tracker = Tracker()
        self.on_start(name, st.tracker)
        out: Outcome | None = None
        try:
            out = self.workers[name](self.ctx, st.tracker, manual)
        except http.Cancelled:
            st.error = "cancelled"
        except Exception as e:  # one stage failing doesn't stop the others
            log.exception("stage %s failed", name)
            st.error = f"{e}"
            traceback.print_exc()
        finally:
            st.running, st.last, st.finished_at = False, out, self.clock()
        self.on_done(name, out, st.error)
        return out

    def run_all(self, manual: bool = False) -> dict[str, Outcome | None]:
        """Every stage once, in order (no threads)."""
        return {name: self.run_stage(name, manual) for name in STAGES}

    # --- threads
    def start(self, fetch: bool = True) -> None:
        """One thread per stage, plus the timer that fetches games (`fetch`: now too, with `update_on_startup`)."""
        if self._threads:
            return
        self._stopping.clear()
        for name in STAGES:
            th = threading.Thread(target=self._loop, args=(name,), name=f"knightly-{name}", daemon=True)
            th.start()
            self._threads.append(th)
        th = threading.Thread(target=self._timer, name="knightly-timer", daemon=True)
        th.start()
        self._threads.append(th)
        if fetch and self.ctx.cfg.extract.update_on_startup:
            self.trigger("extract")
        self._keep_on()

    def _keep_on(self) -> None:
        """Keep on annotating from now, as the settings say (after a cancel, from the next update)."""
        ann = self.ctx.cfg.annotate
        if not ann.keep_annotating:
            self.keep_until = 0.0
        else:
            hours = ann.keep_annotating_hours
            self.keep_until = self.clock() + hours * 3600 if hours > 0 else float("inf")

    @property
    def keeping(self) -> bool:
        """Annotating goes on by itself: allowed now, and the last pass left work."""
        last = self.state["annotate"].last
        return self.clock() < self.keep_until and last is not None and last.more

    def reconfigure(self, cfg: Config) -> None:
        """New settings, for the next runs (a running stage finishes with the ones it started with). The
        stages whose settings changed run again; games are fetched only when the games' settings changed."""
        old, self.ctx.cfg = self.ctx.cfg, cfg
        self.ctx.pool.reconfigure(cfg)
        self._keep_on()
        if old.fingerprint("extract") != cfg.fingerprint("extract"):
            self.trigger("extract")  # and every stage after it
            return
        if old.fingerprint("annotate", "select", "build") != cfg.fingerprint("annotate", "select", "build"):
            for name in ("annotate", "select", "build"):
                self.trigger(name)
        if old.fingerprint("manage", "annotate", "select") != cfg.fingerprint("manage", "annotate", "select"):
            self.trigger("manage")

    def trigger(self, name: str, manual: bool = False) -> None:
        """Run `name` as soon as its thread is free (a run already asked for is not repeated)."""
        self._manual[name] = self._manual[name] or manual
        self._wake[name].set()

    def update(self) -> None:
        """Everything, now: fetch games, then every stage after it (and keep on annotating again)."""
        self._keep_on()
        self.trigger("extract", manual=True)

    def cancel_running(self, wait: float = 10) -> None:
        """Stop what is running now, and keeping on annotating until the next update or new settings; the
        threads stay, ready for the next run."""
        self.keep_until = 0.0
        cancel = self.ctx.cancel
        cancel.set()
        end = time.monotonic() + wait
        while self.busy and time.monotonic() < end:
            time.sleep(0.05)
        self.ctx.cancel = threading.Event()

    def stop(self, wait: float = 0) -> None:
        """Stop the threads; a running stage stops at its next search or download."""
        self._stopping.set()
        self.ctx.cancel.set()
        for e in self._wake.values():
            e.set()
        if wait:
            end = time.monotonic() + wait
            for th in self._threads:
                th.join(max(0.0, end - time.monotonic()))
        self._threads = []

    @property
    def busy(self) -> bool:
        return any(s.running for s in self.state.values())

    def _loop(self, name: str) -> None:
        wake = self._wake[name]
        while not self._stopping.is_set():
            wake.wait()
            if self._stopping.is_set():
                return
            wake.clear()
            manual, self._manual[name] = self._manual[name], False
            self.ctx.background = name == "annotate" and self.state[name].last is not None and not manual
            out = self.run_stage(name, manual)
            if out is None:
                continue
            if name == "extract" or out.progressed:
                for nxt in NEXT.get(name, ()):
                    self.trigger(nxt)
            if name == "annotate" and out.more and self.clock() < self.keep_until:
                self.trigger("annotate")  # keep annotating, at low priority

    def _timer(self) -> None:
        last = self.clock()
        while not self._stopping.wait(30):
            self.ctx.pool.reap(IDLE_ENGINE_SECONDS)  # an idle Stockfish gives its memory back
            hours = self.ctx.cfg.extract.update_every_hours
            if hours > 0 and self.clock() - last >= hours * 3600:
                last = self.clock()
                self.trigger("extract")

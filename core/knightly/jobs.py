"""One update run: extract the games (stages/extract.py) → mine the games ply by ply (pipeline.py) → notes.

`run_update` is the single entry point for the add-on (in a background thread),
the CLI, and later the companion process and a server. It never touches an
Anki collection: it takes the existing notes' PGN in and gives NoteSpecs out.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import http, notes, pipeline, puzzles
from .cache import Cache
from .config import Config
from .engine import broker
from .engine.analyse import Analyser
from .engine.broker import Pool
from .model import Game, NoteSpec
from .progress import Tracker, legacy_listener
from .stages import extract

# progress(stage, done, total, message). Stages: fetch, analyse (puzzles found so far, out
# of max_new_cards), positions (the current step, emitted often), cards.

Progress = Callable[[str, int, int, str], None]


@dataclass
class UpdateResult:
    notes: list[NoteSpec] = field(default_factory=list)  # new notes, and old ones whose Notes field changed
    new_games: int = 0
    analysed_positions: int = 0  # engine searches in this run (any depth)
    new_cards: int = 0
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    finished: bool = False  # the analysis left nothing to continue (every ply done, or enough puzzles)
    skipped: bool = False  # same inputs as the last finished run: nothing was collected or analysed


STATE_KEY = "last_run"


def fingerprint(cfg: Config, game_keys: Iterable[str], exclude: Iterable[str]) -> str:
    """Hash of everything a run depends on: the rules, the settings of the stages it runs (minus secrets; the
    engine's threads and memory change no result), the games used, the positions on cards."""
    settings = cfg.fingerprint("extract", "annotate", "select", "build")
    blob = json.dumps([puzzles.RULES, settings, sorted(game_keys), sorted(exclude)], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _priorities(precalculate: bool) -> dict[str, int]:
    """Pre-calculating is work for later: it waits behind every other user of the engine."""
    if precalculate:
        return {"priority": broker.BACKFILL, "scan_priority": broker.BACKFILL}
    return {"priority": broker.CONFIRM, "scan_priority": broker.SCAN}


def run_update(
    cfg: Config,
    cache: Cache,
    *,
    existing: dict[str, str] | None = None,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    force: bool = False,
    fetch: bool = True,
    analyser_factory: Callable[..., Analyser] | None = None,
    precalculate: bool = False,
    on_note: Callable[[NoteSpec], None] | None = None,
    tracker: Tracker | None = None,
    pool: Pool | None = None,
) -> UpdateResult:
    """`precalculate`: no puzzle cap and no notes made: keep analysing ply by ply so the next update finds
    the searches in the cache. `analysis_minutes` still bounds it.
    `existing` maps note key → PGN of the Knightly notes already in the collection. None: no collection to
    compare with (the CLI), so the saved puzzle records are kept rather than deleted as gone.
    `on_note`: called with a note's whole spec as soon as it is built, so the caller can write it while
    the run goes on: a new puzzle note, or an old one whose Notes field changed (a move played again,
    a similar position). `result.notes` ends with the same specs, and holds those made so far if the run
    is cancelled.
    `tracker`: a progress.Tracker to read the run's progress from (stage, engines, counters) while it goes on;
    `progress` still gets the older `(stage, done, total, message)` calls.
    `pool`: the Stockfish processes shared with other users of the engine (engine/broker.py); None = a pool of
    this run's own."""
    tracker = tracker or Tracker()
    if progress:
        tracker.listen(legacy_listener(progress))
    progress = progress or (lambda *a: None)
    cancel = cancel or threading.Event()
    have_collection = existing is not None
    existing = existing or {}
    result = UpdateResult()
    try:
        if fetch:
            got = extract.run(cfg, cache, force=force, cancel=cancel, message=lambda m: progress("fetch", 0, 0, m))
            result.new_games += got.new_games
            result.warnings += got.warnings

        games: list[Game] = extract.games_of(cfg, cache)  # yours, newest first across the sources
        by_key = {g.key: g for g in games}

        # What is on cards already: puzzles of this pipeline (found again by id: position and ply) and
        # cards from older versions (their positions are never offered again).
        store = puzzles.Store(cache)
        known: list[puzzles.Puzzle] = []
        claimed: set[str] = set()  # the position of every card in the collection: never made twice
        for key, pgn in existing.items():
            claimed |= set(notes.card_ids(pgn))
            if puzzles.parse_key(key):
                p = store.get(key) or puzzles.from_pgn(key, pgn)
                if p is not None:
                    known.append(p)
        for key in store:  # puzzles whose note is gone from the collection may be made again
            if have_collection and key not in existing:
                store.delete(key)
        exclude = claimed

        fp = fingerprint(cfg, by_key, exclude)
        last = cache.get(STATE_KEY) or {}
        if not force and not precalculate and last.get("finished") and last.get("fingerprint") == fp:
            result.skipped = True
            progress("analyse", 0, 0, "Nothing changed since the last update.")
            return result
        if not precalculate:
            cache.set(STATE_KEY, {"fingerprint": fp, "finished": False, "ts": time.time()})

        progress("analyse", 0, 0, f"Collecting the positions of {len(games)} games…")
        an_ctx = (
            analyser_factory(cfg, cache, cancel)
            if analyser_factory
            else Analyser(cfg, cache, cancel, tracker=tracker, pool=pool, **_priorities(precalculate))
        )
        with an_ctx as an:
            miner = pipeline.Miner(
                games,
                an,
                cfg,
                store,
                want=pipeline.UNBOUNDED if precalculate else cfg.build.max_new_cards,
                known=known,
                claimed=claimed,
                existing=existing,
                make=not precalculate,
                tracker=tracker,
                on_note=on_note,
            )
            try:
                miner.run()
            finally:  # a cancelled run keeps the notes it made
                result.notes = miner.notes
                result.new_cards = sum(sp.new_cards for sp in miner.notes)
                result.analysed_positions = miner.analysed
            result.stats = dict(an.stats)
            result.finished = miner.finished
            # Only a run that made no cards counts as finished: new cards change `exclude` anyway, and
            # if they never reach the collection (cancelled, write failed) the next run must try again.
            # The same goes for repaired notes, which the next run has yet to see in the collection.
            if miner.finished and not result.new_cards and not miner.unconfirmed and not precalculate:
                cache.set(STATE_KEY, {"fingerprint": fp, "finished": True, "ts": time.time()})
    except http.Cancelled:
        result.cancelled = True
    return result

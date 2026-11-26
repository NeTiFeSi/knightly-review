"""One update run: fetch → mine the games ply by ply (pipeline.py) → notes.

`run_update` is the single entry point for the add-on (in a background thread),
the CLI, and later the companion process and a server. It never touches an
Anki collection: it takes the existing notes' PGN in and gives NoteSpecs out.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from . import http, notes, pipeline, puzzles, sources
from .cache import Cache, fen_key
from .config import Config
from .engine.analyse import Analyser
from .model import Game, NoteSpec
from .sources import chesscom, lichess

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
    """Hash of everything a run depends on: the settings (minus tokens), the games used, the positions on cards."""
    d = cfg.to_dict()
    d.pop("lichess_token", None)
    blob = json.dumps([d, sorted(game_keys), sorted(exclude)], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def fetch_all(cfg: Config, cache: Cache, *, force: bool, cancel: threading.Event, progress: Progress,
              result: UpdateResult) -> None:
    jobs = []
    if cfg.lichess_user:
        jobs.append(("lichess", cfg.lichess_user))
    if cfg.chesscom_user:
        jobs.append(("chesscom", cfg.chesscom_user))
    for source, user in jobs:
        # A manual update may always ask Lichess (incremental); Chess.com's cache makes
        # frequent asks pointless, so its cooldown always applies.
        mod = lichess if source == "lichess" else chesscom
        backfill = mod.needs_backfill(cache, user, cfg.history_days)  # the look-back grew
        if not sources.due(cache, source, user) and not (force and source == "lichess") and not backfill:
            continue
        msg = lambda m: progress("fetch", 0, 0, m)  # noqa: E731
        msg(f"Fetching games from {'Lichess' if source == 'lichess' else 'Chess.com'}…")
        try:
            if source == "lichess":
                n = lichess.fetch(cache, user, token=cfg.lichess_token, history_days=cfg.history_days,
                                  time_classes=cfg.time_classes, rated_only=cfg.rated_only, cancel=cancel,
                                  progress=msg)
            else:
                n = chesscom.fetch(cache, user, history_days=cfg.history_days, time_classes=cfg.time_classes,
                                   rated_only=cfg.rated_only, cancel=cancel, progress=msg)
            result.new_games += n
            sources.mark_fetched(cache, source, user)
        except http.Cancelled:
            raise
        except Exception as e:  # one site failing shouldn't stop the other
            result.warnings.append(f"{source}: {e}")


def run_update(
    cfg: Config,
    cache: Cache,
    *,
    existing: Optional[dict[str, str]] = None,
    progress: Optional[Progress] = None,
    cancel: Optional[threading.Event] = None,
    force: bool = False,
    fetch: bool = True,
    analyser_factory: Optional[Callable[..., Analyser]] = None,
    gate: Optional[threading.Event] = None,
    precalculate: bool = False,
    on_note: Optional[Callable[[NoteSpec], None]] = None,
) -> UpdateResult:
    """`gate`: analysis waits while it is cleared (live play during review takes priority).
    `precalculate`: no puzzle cap and no notes made: keep analysing ply by ply so the next update finds
    the searches in the cache. `analysis_minutes` still bounds it.
    `existing` maps note key → PGN of the Knightly notes already in the collection.
    `on_note`: called with a note's whole spec as soon as it is built, so the caller can write it while
    the run goes on: a new puzzle note, or an old one whose Notes field changed (a move played again,
    a similar position). `result.notes` ends with the same specs, and holds those made so far if the run
    is cancelled."""
    progress = progress or (lambda *a: None)
    cancel = cancel or threading.Event()
    existing = existing or {}
    result = UpdateResult()
    try:
        if fetch:
            fetch_all(cfg, cache, force=force, cancel=cancel, progress=progress, result=result)

        since = sources.since_ts(cfg.history_days)
        games: list[Game] = []
        for source, user in (("lichess", cfg.lichess_user), ("chesscom", cfg.chesscom_user)):
            if user:
                games += [g for g in cache.games(source, user, since) if not cfg.time_classes or g.time_class in cfg.time_classes]
        games.sort(key=lambda g: g.end_ts, reverse=True)  # newest first across both sites
        by_key = {g.key: g for g in games}

        # What is on cards already: puzzles of this pipeline (found again by id: position and ply) and
        # cards from older versions (their positions are never offered again).
        store = puzzles.Store(cache)
        known: list[puzzles.Puzzle] = []
        legacy: set[str] = set()
        for key, pgn in existing.items():
            if puzzles.parse_key(key):
                p = store.get(key) or puzzles.from_pgn(key, pgn)
                if p is not None:
                    known.append(p)
                    continue
            legacy |= set(notes.card_ids(pgn))
        for key in store.keys():  # puzzles whose note is gone from the collection may be made again
            if key not in existing:
                store.delete(key)
        exclude = legacy | {fen_key(p.fen) for p in known}

        fp = fingerprint(cfg, by_key, exclude)
        last = cache.get(STATE_KEY) or {}
        if not force and not precalculate and last.get("finished") and last.get("fingerprint") == fp:
            result.skipped = True
            progress("analyse", 0, 0, "Nothing changed since the last update.")
            return result
        if not precalculate:
            cache.set(STATE_KEY, {"fingerprint": fp, "finished": False, "ts": time.time()})

        progress("analyse", 0, 0, f"Collecting the positions of {len(games)} games…")
        with (analyser_factory or Analyser)(cfg, cache, cancel) as an:
            an.gate = gate
            miner = pipeline.Miner(games, an, cfg, store, want=pipeline.UNBOUNDED if precalculate else cfg.max_new_cards,
                                   known=known, legacy=legacy, existing=existing, make=not precalculate,
                                   progress=progress, on_note=on_note)
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
            if miner.finished and not result.new_cards and not precalculate:
                cache.set(STATE_KEY, {"fingerprint": fp, "finished": True, "ts": time.time()})
    except http.Cancelled:
        result.cancelled = True
    return result

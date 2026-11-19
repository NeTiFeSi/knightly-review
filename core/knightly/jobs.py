"""One update run: fetch → quick analysis → select → deep analysis → notes.

`run_update` is the single entry point for the add-on (in a background thread),
the CLI, and later the companion process and a server. It never touches an
Anki collection: it takes the existing notes' PGN in and gives NoteSpecs out.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import chess

from . import http, notes, sources
from .cache import Cache
from .config import Config
from .engine.analyse import Analyser
from .model import Game, NoteSpec, Score
from .select import build_card, find_candidates
from .sources import chesscom, lichess

# progress(stage, done, total, message)
Progress = Callable[[str, int, int, str], None]


@dataclass
class UpdateResult:
    notes: list[NoteSpec] = field(default_factory=list)
    new_games: int = 0
    analysed_games: int = 0
    new_cards: int = 0
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _opponent(g: Game) -> str:
    return (g.black if g.user_is_white else g.white) or "Your opponent"


def _scores(raw: list) -> list[Optional[Score]]:
    return [Score.from_json(s) if s else None for s in raw]


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
        if not sources.due(cache, source, user) and not (force and source == "lichess"):
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
) -> UpdateResult:
    """`gate`: analysis waits while it is cleared (live play during review takes priority)."""
    """`existing` maps note key → PGN of notes already in the collection."""
    progress = progress or (lambda *a: None)
    cancel = cancel or threading.Event()
    existing = existing or {}
    result = UpdateResult()
    try:
        if fetch:
            fetch_all(cfg, cache, force=force, cancel=cancel, progress=progress, result=result)

        since = int(time.time() - cfg.history_days * 86400)
        games: list[Game] = []
        for source, user in (("lichess", cfg.lichess_user), ("chesscom", cfg.chesscom_user)):
            if user:
                games += [g for g in cache.games(source, user, since) if not cfg.time_classes or g.time_class in cfg.time_classes]
        games.sort(key=lambda g: g.end_ts, reverse=True)  # newest first across both sites
        by_key = {g.key: g for g in games}

        # Quick pass over games not analysed yet, newest first, a bounded number per run.
        todo = [g for g in games if cache.get_analysis(g) is None][: cfg.max_games_per_run]
        with (analyser_factory or Analyser)(cfg, cache, cancel) as an:
            an.gate = gate
            for i, g in enumerate(todo):
                progress("analyse", i, len(todo), f"Analysing game {i + 1} of {len(todo)} ({g.white} – {g.black})")
                scores = an.analyse_game(g)
                cache.put_analysis(g, [s.to_json() if s else None for s in scores])
                result.analysed_games += 1

            # Positions already on cards (in any note) are never picked again.
            carded: dict[str, dict[str, int]] = {k: notes.card_ids(p) for k, p in existing.items()}
            exclude = {fk for ids in carded.values() for fk in ids}
            analysed = []
            for g in games:
                raw = cache.get_analysis(g)
                if raw is not None:
                    analysed.append((g, _scores(raw)))
            cands = find_candidates(analysed, cfg, exclude)

            # Deep pass on the best candidates until we have enough cards.
            per_game: dict[str, list] = {}
            made = 0
            for c in cands:
                if made >= cfg.max_new_cards:
                    break
                progress("cards", made, cfg.max_new_cards, f"Checking a candidate puzzle ({made} of {cfg.max_new_cards} found)")
                g = by_key[c.game_key]
                board = chess.Board()
                for u in g.moves[: c.ply]:
                    board.push_uci(u)
                card = build_card(board, c.ply, c.played, an, cfg, "" if c.mine else _opponent(g))
                if card:
                    per_game.setdefault(g.key, []).append(card)
                    made += 1

            # A game that already has a note gets its old cards rebuilt (from the cache) plus the new ones.
            for key, new_cards in per_game.items():
                g = by_key[key]
                old_ids = carded.get(key, {})
                cards = list(new_cards)
                if old_ids:
                    board = chess.Board()
                    want = dict(old_ids)
                    for ply, u in enumerate(g.moves):
                        fk = " ".join(board.fen().split()[:4])
                        if fk in want:
                            mine = board.turn == (chess.WHITE if g.user_is_white else chess.BLACK)
                            card = build_card(board.copy(stack=False), ply, u, an, cfg, "" if mine else _opponent(g))
                            if card:
                                cards.append(card)
                                del want[fk]
                        board.push_uci(u)
                    if want:
                        result.warnings.append(f"{key}: could not rebuild {len(want)} existing card(s); note left unchanged")
                        continue
                raw = cache.get_analysis(g) or []
                spec = notes.build(g, _scores(raw), cards, old_ids)
                result.notes.append(spec)
                result.new_cards += spec.new_cards
            result.stats = dict(an.stats)
    except http.Cancelled:
        result.cancelled = True
    return result

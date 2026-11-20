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

from . import http, notes, openings, sources
from .cache import Cache, fen_key
from .config import Config
from .engine.analyse import Analyser
from .model import CardSpec, Game, NoteSpec, Score
from .openings import Group
from .select import build_card, find_candidates, opening_candidates, parse_opening_note_key
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
    # Opening mode: every opening note (new and existing) in the order its new cards should
    # be studied. The add-on repositions the new cards to match. Empty in game mode.
    order: list[str] = field(default_factory=list)


def _opponent(g: Game) -> str:
    return (g.black if g.user_is_white else g.white) or "Your opponent"


def _scores(raw: list) -> list[Optional[Score]]:
    return [Score.from_json(s) if s else None for s in raw]


def _game_note(key: str, new: list[tuple[Game, CardSpec]], old_ids: dict[str, int], by_key: dict[str, Game],
               scored: dict[str, list[Optional[Score]]], an, cfg: Config, result: UpdateResult) -> Optional[NoteSpec]:
    """A game's note: its old cards rebuilt (deep evals come from the cache) plus the new ones."""
    g = by_key[key]
    cards = [c for _, c in new]
    if old_ids:
        board = chess.Board()
        want = dict(old_ids)
        for ply, u in enumerate(g.moves):
            fk = fen_key(board.fen())
            if fk in want:
                mine = board.turn == (chess.WHITE if g.user_is_white else chess.BLACK)
                card = build_card(board.copy(stack=False), ply, u, an, cfg, "" if mine else _opponent(g))
                if card:
                    cards.append(card)
                    del want[fk]
            board.push_uci(u)
        if want:
            result.warnings.append(f"{key}: could not rebuild {len(want)} existing card(s); note left unchanged")
            return None
    return notes.build(g, scored.get(g.key, []), cards, old_ids)


def _opening_note(key: str, new: list[tuple[Game, CardSpec]], groups: list[Group],
                  scored: dict[str, list[Optional[Score]]], existing_pgn: str) -> NoteSpec:
    """An opening+ply note: the games already in it kept as they are, plus one game per new card."""
    color, slug, ply = parse_opening_note_key(key)  # type: ignore[misc]
    label = next((gr.label for gr in groups if (gr.color, gr.slug) == (color, slug)), slug)
    entries = [(g, scored.get(g.key, []), c) for g, c in new]
    return notes.build_opening(key, label, ply, entries, existing_pgn)


def opening_order(groups: list[Group], keys: set[str]) -> list[str]:
    """Opening note keys by (opening importance, ply). Openings no longer ranked go last."""
    rank = {(gr.color, gr.slug): i for i, gr in enumerate(groups)}
    parsed = [(k, parse_opening_note_key(k)) for k in keys]
    return [k for k, p in sorted(((k, p) for k, p in parsed if p),
                                 key=lambda kp: (rank.get(kp[1][:2], len(rank)), kp[1][0], kp[1][1], kp[1][2]))]


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
            scored = {g.key: _scores(raw) for g in games if (raw := cache.get_analysis(g)) is not None}
            by_opening = cfg.note_grouping == "opening"
            if by_opening:
                groups = openings.rank(games)
                cands = opening_candidates(groups, scored, cfg, exclude)
                min_loss: Optional[float] = cfg.opening_min_loss
            else:
                cands = find_candidates([(g, scored[g.key]) for g in games if g.key in scored], cfg, exclude)
                min_loss = None

            # Deep pass, in the order above, until we have enough cards.
            per_note: dict[str, list[tuple[Game, CardSpec]]] = {}
            made = 0
            for c in cands:
                if made >= cfg.max_new_cards:
                    break
                progress("cards", made, cfg.max_new_cards, f"Checking a candidate puzzle ({made} of {cfg.max_new_cards} found)")
                g = by_key[c.game_key]
                board = chess.Board()
                for u in g.moves[: c.ply]:
                    board.push_uci(u)
                card = build_card(board, c.ply, c.played, an, cfg, "" if c.mine else _opponent(g), min_loss)
                if card:
                    per_note.setdefault(c.note, []).append((g, card))
                    made += 1

            for key, new in per_note.items():
                if by_opening:
                    spec = _opening_note(key, new, groups, scored, existing.get(key, ""))
                else:
                    spec = _game_note(key, new, carded.get(key, {}), by_key, scored, an, cfg, result)
                if spec:
                    result.notes.append(spec)
                    result.new_cards += spec.new_cards
            if by_opening:
                result.order = opening_order(groups, set(existing) | set(per_note))
            result.stats = dict(an.stats)
    except http.Cancelled:
        result.cancelled = True
    return result

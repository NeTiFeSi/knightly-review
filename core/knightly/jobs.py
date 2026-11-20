"""One update run: fetch → analysis by increasing depth (deepen.py) → select → notes.

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

import chess

from . import deepen, http, notes, openings, sources
from .cache import Cache, fen_key
from .config import Config
from .engine.analyse import Analyser
from .model import CardSpec, Game, NoteSpec, Score
from .openings import Group
from .select import (build_card, find_candidates, opening_candidates, parse_game_note_key, parse_opening_note_key,
                     ranked_candidates)
from .sources import chesscom, lichess

# progress(stage, done, total, message). Stages: fetch, analyse (puzzles found so far, out
# of max_new_cards), positions (the current step, emitted often), cards.
Progress = Callable[[str, int, int, str], None]


@dataclass
class UpdateResult:
    notes: list[NoteSpec] = field(default_factory=list)
    new_games: int = 0
    analysed_positions: int = 0  # engine searches in this run (any depth)
    new_cards: int = 0
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    # Opening mode: every opening note (new and existing) in the order its new cards should
    # be studied. The add-on repositions the new cards to match. Empty in game mode.
    order: list[str] = field(default_factory=list)
    finished: bool = False  # the analysis left nothing to continue (every position at full depth, or enough cards)
    skipped: bool = False  # same inputs as the last finished run: nothing was collected or analysed


STATE_KEY = "last_run"
PRECALC_KEY = "confirmed"
# Settings that don't change which moves are worth a card, so they don't invalidate saved candidates.
_PRECALC_IGNORED = ("lichess_token", "max_new_cards", "analysis_minutes", "keep_running", "keep_running_hours",
                    "lichess_user", "chesscom_user", "history_days", "time_classes", "rated_only")


def precalc_signature(cfg: Config) -> str:
    d = {k: v for k, v in cfg.to_dict().items() if k not in _PRECALC_IGNORED}
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def fingerprint(cfg: Config, game_keys: Iterable[str], exclude: Iterable[str]) -> str:
    """Hash of everything a run depends on: the settings (minus tokens), the games used, the positions on cards."""
    d = cfg.to_dict()
    d.pop("lichess_token", None)
    blob = json.dumps([d, sorted(game_keys), sorted(exclude)], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _opponent(g: Game) -> str:
    return (g.black if g.user_is_white else g.white) or "Your opponent"


def _game_note(key: str, new: list[tuple[Game, CardSpec]], old_ids: dict[str, int], by_key: dict[str, Game],
               scored: dict[str, list[Optional[Score]]], an, cfg: Config, result: UpdateResult) -> Optional[NoteSpec]:
    """A game's note for one phase: its old cards rebuilt (deep evals come from the cache) plus the new ones."""
    parsed = parse_game_note_key(key)
    if parsed is None or parsed[0] not in by_key:
        result.warnings.append(f"{key}: no such game in the look-back; note left unchanged")
        return None
    g = by_key[parsed[0]]
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


def _card_rank(cfg: Config, by_key: dict[str, Game], groups: list[Group]) -> deepen.Rank:
    """The order candidates are confirmed in, close to the order the note grouping makes cards in."""
    if cfg.note_grouping == "opening":
        group_of = {g.key: i for i, gr in enumerate(groups) for g in gr.games}
        return lambda e, loss, count: (group_of.get(e.game_key, len(groups)), e.ply)
    if cfg.note_grouping == "ranked":  # select.ranked_candidates' score
        def ranked(e: deepen.Edge, loss: float, count: int) -> tuple:
            weight = (1.0 if by_key[e.game_key].user_result() < 1 else 0.6) if e.mine else 0.8
            return (-loss * weight * (1 + 0.5 * (count - 1)),)
        return ranked
    return lambda e, loss, count: (-by_key[e.game_key].end_ts, e.ply)  # newest game first


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
) -> UpdateResult:
    """`gate`: analysis waits while it is cleared (live play during review takes priority).
    `precalculate`: no card cap and no cards made: keep deepening and confirming candidates, and save them
    (as every run does) so the next update adds cards right away. `analysis_minutes` still bounds it.
    `existing` maps note key → PGN of notes already in the collection."""
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

        # Positions already on cards (in any note) are never picked again.
        carded: dict[str, dict[str, int]] = {k: notes.card_ids(p) for k, p in existing.items()}
        exclude = {fk for ids in carded.values() for fk in ids}
        by_opening = cfg.note_grouping == "opening"
        threshold = cfg.opening_min_loss if by_opening else cfg.min_loss
        min_loss: Optional[float] = cfg.opening_min_loss if by_opening else None

        fp = fingerprint(cfg, by_key, exclude)
        last = cache.get(STATE_KEY) or {}
        if not force and not precalculate and last.get("finished") and last.get("fingerprint") == fp:
            result.skipped = True
            progress("analyse", 0, 0, "Nothing changed since the last update.")
            return result
        if not precalculate:
            cache.set(STATE_KEY, {"fingerprint": fp, "finished": False, "ts": time.time()})
        sig = precalc_signature(cfg)
        saved = cache.get(PRECALC_KEY) or {}
        known = [tuple(c) for c in saved.get("confirmed", [])] if saved.get("signature") == sig else []

        progress("analyse", 0, 0, f"Collecting the positions of {len(games)} games…")
        positions = deepen.collect(games, cfg, exclude)
        groups = openings.rank(games) if by_opening else []
        with (analyser_factory or Analyser)(cfg, cache, cancel) as an:
            an.gate = gate
            # Step-by-step analysis, most-seen positions first; see deepen.py.
            dp = deepen.Deepener(positions, an, cfg, threshold=threshold, card_min_loss=min_loss,
                                 opponents={g.key: _opponent(g) for g in games},
                                 want=deepen.UNBOUNDED if precalculate else cfg.max_new_cards,
                                 rank=_card_rank(cfg, by_key, groups), progress=progress, confirmed=known)
            dp.run()
            result.analysed_positions = dp.analysed
            cache.set(PRECALC_KEY, {"signature": sig, "confirmed": sorted(dp.confirmed | set(known))})
            if precalculate:
                result.stats = dict(an.stats)
                result.finished = dp.finished
                return result

            # The mode decides the order among the confirmed moves.
            scored = {g.key: dp.scores_of(g.key) for g in games}
            if by_opening:
                cands = opening_candidates(groups, scored, cfg, exclude)
            else:
                pick = ranked_candidates if cfg.note_grouping == "ranked" else find_candidates
                cands = pick([(g, scored[g.key]) for g in games], cfg, exclude)
            cands = [c for c in cands if (c.fen_key, c.played) in dp.confirmed]

            # Deep pass, in the order above, until we have enough cards.
            per_note: dict[str, list[tuple[Game, CardSpec]]] = {}
            made = 0
            for c in cands:
                if made >= cfg.max_new_cards:
                    break
                progress("cards", made, cfg.max_new_cards, f"Building the cards ({made} of {cfg.max_new_cards})")
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
            # Only a run that made no cards counts as finished: new cards change `exclude` anyway, and
            # if they never reach the collection (cancelled, write failed) the next run must try again.
            result.finished = dp.finished
            if dp.finished and not result.new_cards:
                cache.set(STATE_KEY, {"fingerprint": fp, "finished": True, "ts": time.time()})
    except http.Cancelled:
        result.cancelled = True
    return result

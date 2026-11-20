"""Analysis by increasing depth, most-seen positions first.

1. Every position of every game (in the look-back) is collected once, with how many
   games reached it (its recurrence).
2. Every position gets a Stockfish score at depth 1, most-seen first.
3. A move is a *candidate* mistake when it loses at least half the threshold at the
   current depths of its two positions (the full threshold once both are at the
   configured depth, as the old quick pass did). Candidates climb first, a batch at a
   time, in the order the cards will be made in (`rank`, from the note grouping; then
   most-seen, then biggest loss): their positions go to depth 2, the ones still losing
   enough to depth 3, and so on up to `cfg.sf_depth`. Until then only Stockfish runs,
   with one line.
4. At the configured depth a candidate gets the full setup (Lc0 too when on, every
   line): `build_card`. If it makes a card, the move is *confirmed*.
5. When no candidates are left, the other positions keep climbing, a level at a time,
   most-seen first, which can reveal new candidates.

A run stops once enough moves are confirmed or its time is up. Scores are cached per
position and depth, so the next run carries on where this one stopped.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import chess

from .cache import fen_key
from .config import Config
from .engine.analyse import terminal
from .model import Game, Score
from .select import build_card

DONE = 10_000  # depth given to finished positions (mate, stalemate…): nothing to search
BACKGROUND_CHUNK = 200  # positions deepened between two looks at the candidates


@dataclass(eq=False)
class Edge:
    """A move from `before` to `after`, as first seen in the newest game that played it."""

    before: str
    after: str
    played: str
    white: bool  # the mover
    mine: bool
    game_key: str
    ply: int


@dataclass
class Positions:
    fen: dict[str, str] = field(default_factory=dict)  # fen key → a full FEN to analyse
    count: dict[str, int] = field(default_factory=dict)  # games that reached the position
    keys: dict[str, list[str]] = field(default_factory=dict)  # game key → fen key at each ply
    edges: list[Edge] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # most-seen first


def collect(games: Iterable[Game], cfg: Config, exclude: set[str] = frozenset()) -> Positions:
    """Positions of `games` (newest first), and the moves that could become cards: the user's
    (and the opponent's with `cfg.include_opponent`), from positions not on a card yet, with
    a choice of moves."""
    p = Positions()
    seen_edges: set[tuple[str, str, bool]] = set()
    for g in games:
        board = chess.Board()
        user = chess.WHITE if g.user_is_white else chess.BLACK
        ks = [fen_key(board.fen())]
        movers: list[tuple[bool, bool, bool]] = []  # (white, mine, a choice of moves)
        for u in g.moves:
            if ks[-1] not in p.fen:
                p.fen[ks[-1]] = board.fen()
            movers.append((board.turn == chess.WHITE, board.turn == user, board.legal_moves.count() > 1))
            board.push_uci(u)
            ks.append(fen_key(board.fen()))
        if ks[-1] not in p.fen:
            p.fen[ks[-1]] = board.fen()
        p.keys[g.key] = ks
        for k in set(ks):
            p.count[k] = p.count.get(k, 0) + 1
        for ply, (white, mine, choice) in enumerate(movers):
            if not choice or not (mine or cfg.include_opponent) or ks[ply] in exclude:
                continue
            ek = (ks[ply], g.moves[ply], mine)
            if ek not in seen_edges:
                seen_edges.add(ek)
                p.edges.append(Edge(ks[ply], ks[ply + 1], g.moves[ply], white, mine, g.key, ply))
    p.order = sorted(p.fen, key=lambda k: -p.count[k])  # stable: ties keep game order
    return p


Progress = Callable[[str, int, int, str], None]
# Card order of a candidate (smaller first): (edge, its current loss, its recurrence) → key.
Rank = Callable[[Edge, float, int], tuple]


class Deepener:
    def __init__(self, positions: Positions, analyser, cfg: Config, *, threshold: float,
                 card_min_loss: Optional[float], opponents: dict[str, str], want: int,
                 rank: Optional[Rank] = None, progress: Optional[Progress] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.p, self.an, self.cfg = positions, analyser, cfg
        self.threshold = threshold
        self.card_min_loss = card_min_loss
        self.opponents = opponents  # game key → the opponent's name (for opponent-mistake cards)
        self.want = want
        self.rank = rank or (lambda e, loss, count: ())
        self.progress = progress or (lambda *a: None)
        self.clock = clock
        self.deadline = clock() + cfg.analysis_minutes * 60 if cfg.analysis_minutes > 0 else math.inf
        self.full = max(1, cfg.sf_depth)
        self.depth: dict[str, int] = {}
        self.score: dict[str, Score] = {}
        self.by_pos: dict[str, list[Edge]] = {}
        for e in positions.edges:
            self.by_pos.setdefault(e.before, []).append(e)
            self.by_pos.setdefault(e.after, []).append(e)
        self.pending: set[Edge] = set()  # candidates not yet tried at full depth
        self.tried: set[Edge] = set()
        self.confirmed: set[tuple[str, str]] = set()  # (fen key, move) that made a card
        self.analysed = 0  # positions searched by the engine in this run
        self._bg_level, self._bg_list, self._bg_i = 1, [], 0
        self._step = ""

    # --- state
    def out_of_time(self) -> bool:
        return self.clock() >= self.deadline

    def level(self, e: Edge) -> int:
        return min(self.depth.get(e.before, 0), self.depth.get(e.after, 0))

    def loss(self, e: Edge) -> Optional[float]:
        a, b = self.score.get(e.before), self.score.get(e.after)
        if a is None or b is None:
            return None
        return a.win_pct(e.white) - b.win_pct(e.white)

    def is_candidate(self, e: Edge) -> bool:
        loss = self.loss(e)
        if loss is None:
            return False
        return loss >= (self.threshold if self.level(e) >= self.full else self.threshold / 2)

    def _recheck(self, k: str) -> None:
        for e in self.by_pos.get(k, ()):
            if e not in self.tried and self.is_candidate(e):
                self.pending.add(e)
            else:
                self.pending.discard(e)

    def _priority(self, e: Edge) -> tuple:
        loss, count = self.loss(e) or 0.0, self.p.count[e.before]
        return (*self.rank(e, loss, count), -count, -loss)

    def scores_of(self, game_key: str) -> list[Optional[Score]]:
        return [self.score.get(k) for k in self.p.keys.get(game_key, [])]

    # --- reporting
    def _report(self, done: int, total: int, step: str) -> None:
        if step != self._step:
            self._step = step
            left = self.deadline - self.clock()
            budget = "" if left == math.inf else f" · {max(0, math.ceil(left / 60))} min left"
            found = len(self.confirmed)
            self.progress("analyse", found, self.want, f"{step} ({found} of {self.want} puzzles found{budget})")
        self.progress("positions", done, total, step)

    # --- work
    def _deepen(self, keys: list[str], depth: int, step: str) -> None:
        todo = [k for k in keys if self.depth.get(k, 0) < depth]
        for i, k in enumerate(todo):
            if self.out_of_time():
                return
            self._report(i, len(todo), step)
            board = chess.Board(self.p.fen[k])
            if board.is_game_over():
                s, d = terminal(board), DONE
            else:
                s, d = self.an.score(board, depth)
                self.analysed += 1
            self.score[k], self.depth[k] = s, d
            self._recheck(k)
        self._report(len(todo), len(todo), step)

    def _confirm(self, e: Edge) -> None:
        self.tried.add(e)
        self.pending.discard(e)
        board = chess.Board(self.p.fen[e.before])
        opponent = "" if e.mine else self.opponents.get(e.game_key, "Your opponent")
        if build_card(board, e.ply, e.played, self.an, self.cfg, opponent, self.card_min_loss):
            self.confirmed.add((e.before, e.played))
        self._step = ""  # report the new count

    def _climb(self, batch: list[Edge]) -> None:
        """Take a batch of candidates up a level at a time, dropping those that stop losing enough."""
        for d in range(min(self.level(e) for e in batch) + 1, self.full + 1):
            batch = [e for e in batch if e in self.pending]
            need = list(dict.fromkeys(k for e in batch for k in (e.before, e.after) if self.depth.get(k, 0) < d))
            if need:
                self._deepen(need, d, f"Checking {len(batch)} possible mistakes at depth {d} of {self.full}")
            if self.out_of_time():
                return
        live = sorted((e for e in batch if e in self.pending), key=self._priority)
        for i, e in enumerate(live):
            if self.out_of_time() or len(self.confirmed) >= self.want:
                return
            self._report(i, len(live), f"Making puzzles at full strength ({len(live)} moves)")
            self._confirm(e)

    def _background(self) -> bool:
        """Deepen the next chunk of other positions. False when everything is at full depth."""
        while True:
            if self._bg_i >= len(self._bg_list):
                if self._bg_level >= self.full:
                    return False
                self._bg_level += 1
                self._bg_list = [k for k in self.p.order if self.depth.get(k, 0) < self._bg_level]
                self._bg_i = 0
                continue
            chunk = self._bg_list[self._bg_i : self._bg_i + BACKGROUND_CHUNK]
            self._bg_i += BACKGROUND_CHUNK
            if any(self.depth.get(k, 0) < self._bg_level for k in chunk):
                self._deepen(chunk, self._bg_level,
                             f"Deepening the other positions to depth {self._bg_level} of {self.full}")
                return True

    def run(self) -> None:
        for k, (d, s) in self.an.known(self.p.fen).items():
            self.depth[k], self.score[k] = d, s
        self._deepen(self.p.order, 1, "Depth 1 for every position")
        for k in self.p.fen:
            self._recheck(k)
        batch_size = max(10, 3 * self.want)
        while not self.out_of_time() and len(self.confirmed) < self.want:
            if self.pending:
                self._climb(heapq.nsmallest(batch_size, self.pending, key=self._priority))
            elif not self._background():
                break

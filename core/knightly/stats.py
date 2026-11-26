"""Which positions the games reach at each ply, and how often each move was played there.

A puzzle's id is its position plus its ply (half-moves already played). Everything here is
recomputed from the games in the look-back on every run, so adding games just updates the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import chess

from .cache import fen_key
from .model import Game


@dataclass(eq=False)
class MoveStat:
    mine: int = 0  # times the user played it here
    opp: int = 0  # times an opponent played it here
    mine_game: Optional[Game] = None  # the newest game where the user played it
    opp_game: Optional[Game] = None

    @property
    def total(self) -> int:
        return self.mine + self.opp


@dataclass(eq=False)
class Pos:
    """A position at one ply, with the moves played from it."""

    fen: str  # a full FEN
    ply: int
    moves: dict[str, MoveStat] = field(default_factory=dict)  # UCI → counts, first seen (newest game) first

    @property
    def key(self) -> str:
        return fen_key(self.fen)

    @property
    def reached(self) -> int:
        return sum(m.total for m in self.moves.values())


def collect(games: Iterable[Game]) -> dict[int, dict[str, Pos]]:
    """ply → fen key → position, for every move of `games` (newest first, so first seen = newest)."""
    out: dict[int, dict[str, Pos]] = {}
    for g in games:
        board = chess.Board()
        user = chess.WHITE if g.user_is_white else chess.BLACK
        for ply, u in enumerate(g.moves):
            k = fen_key(board.fen())
            pos = out.setdefault(ply, {}).get(k)
            if pos is None:
                pos = out[ply][k] = Pos(board.fen(), ply)
            st = pos.moves.setdefault(u, MoveStat())
            if board.turn == user:
                st.mine += 1
                st.mine_game = st.mine_game or g
            else:
                st.opp += 1
                st.opp_game = st.opp_game or g
            board.push_uci(u)
    return out


# ---------------------------------------------------------------- explorer: per position, whatever the ply


@dataclass
class MoveResults:
    """How often a move was played from a position (in how many games) and how those games ended."""

    games: int = 0
    white: int = 0  # games White won
    draw: int = 0
    black: int = 0  # games Black won

    @property
    def decided(self) -> int:
        """Games with a result (an unfinished game counts as played, not as a result)."""
        return self.white + self.draw + self.black


@dataclass
class Position:
    """A position as the games reached it, matched by fen key whatever the ply (transpositions merge)."""

    fen: str
    games: set[str] = field(default_factory=set)  # distinct games that reached it
    moves: dict[str, MoveResults] = field(default_factory=dict)  # UCI → results, first seen first
    results: dict[str, str] = field(default_factory=dict)  # game key → its result

    @property
    def reached(self) -> int:
        return len(self.games)

    def totals(self) -> MoveResults:
        """The results of every game that reached the position."""
        t = MoveResults(games=self.reached)
        for r in self.results.values():
            t.white += r == "1-0"
            t.draw += r == "1/2-1/2"
            t.black += r == "0-1"
        return t


def collect_positions(games: Iterable[Game]) -> dict[str, Position]:
    """fen key → position, with the moves played from it and their results, over `games`. A game counts once
    for a position and once for each move played from it, even when the position repeats in the game."""
    out: dict[str, Position] = {}
    for g in games:
        board = chess.Board()
        seen: set[str] = set()
        seen_moves: set[tuple[str, str]] = set()
        for u in g.moves:
            k = fen_key(board.fen())
            pos = out.get(k)
            if pos is None:
                pos = out[k] = Position(board.fen())
            if k not in seen:
                seen.add(k)
                pos.games.add(g.key)
                pos.results[g.key] = g.result
            if (k, u) not in seen_moves:
                seen_moves.add((k, u))
                m = pos.moves.setdefault(u, MoveResults())
                m.games += 1
                m.white += g.result == "1-0"
                m.draw += g.result == "1/2-1/2"
                m.black += g.result == "0-1"
            board.push_uci(u)
        # The position after the last move has no move from it, but the game reached it.
        k = fen_key(board.fen())
        pos = out.get(k)
        if pos is None:
            pos = out[k] = Position(board.fen())
        if g.key not in pos.games:
            pos.games.add(g.key)
            pos.results[g.key] = g.result
    return out

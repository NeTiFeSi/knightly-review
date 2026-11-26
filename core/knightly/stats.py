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


def lines(pos: Optional[Pos], label: str = "") -> list[str]:
    """One HTML line per move played more than once from `pos`, most played first:
    'Nf3: played 7 times (you 5, opponents 2; 58% of the 12 games that reached this position).'"""
    if pos is None:
        return []
    board = chess.Board(pos.fen)
    total = pos.reached
    out = []
    for u, st in sorted(pos.moves.items(), key=lambda kv: -kv[1].total):
        if st.total < 2:
            continue
        try:
            san = board.san(chess.Move.from_uci(u))
        except ValueError:
            continue
        out.append(f"{label}{san}: played {st.total} times (you {st.mine}, opponents {st.opp}; "
                   f"{round(100 * st.total / total)}% of the {total} games that reached this position).")
    return out

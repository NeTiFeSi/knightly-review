"""Opening, middlegame or endgame: Lichess's game divider.

A port of scalachess's `Divider` (lichess-org/scalachess, core/src/main/scala/Divider.scala,
MIT licence). The middlegame starts at the first position where at most 10 major and
minor pieces are left, a back rank has thinned out (fewer than 4 pieces of its own
colour), or the pieces are mixed up (the `_mixedness` score is over 150). The endgame
starts at the first position with at most 6 major and minor pieces, and only counts in
a game that has a middlegame. Phases are decided for the whole game, so once a game is in
the middlegame it never goes back to the opening.
"""

from __future__ import annotations

from collections.abc import Iterable

import chess

OPENING, MIDDLEGAME, ENDGAME = "opening", "middlegame", "endgame"
PHASES = (OPENING, MIDDLEGAME, ENDGAME)

Division = tuple[int | None, int | None]  # (middlegame start ply, endgame start ply)

_REGIONS = [0x0303 << (x + 8 * y) for y in range(7) for x in range(7)]  # 2×2 squares


def _majors_and_minors(board: chess.Board) -> int:
    return chess.popcount(board.occupied & ~(board.kings | board.pawns))


def _backrank_sparse(board: chess.Board) -> bool:
    """A thin back rank means the pieces have been developed."""
    return (
        chess.popcount(chess.BB_RANK_1 & board.occupied_co[chess.WHITE]) < 4
        or chess.popcount(chess.BB_RANK_8 & board.occupied_co[chess.BLACK]) < 4
    )


def _score(y: int, white: int, black: int) -> int:
    if white == 0:
        return {
            1: 1 + y,
            2: 2 + (6 - y) if y < 6 else 0,
            3: 3 + (7 - y) if y < 7 else 0,
            4: 3 + (7 - y) if y < 7 else 0,
        }.get(black, 0)
    if white == 1:
        return {0: 1 + (8 - y), 1: 5 + abs(4 - y), 2: 4 + (7 - y), 3: 5 + (7 - y)}.get(black, 0)
    if white == 2:
        return {0: 2 + (y - 2) if y > 2 else 0, 1: 4 + (y - 1), 2: 7}.get(black, 0)
    if white == 3:
        return {0: 3 + (y - 1) if y > 1 else 0, 1: 5 + (y - 1)}.get(black, 0)
    if white == 4:
        return (3 + (y - 1) if y > 1 else 0) if black == 0 else 0  # a group of 4 on the home rank = 0
    return 0


def _mixedness(board: chess.Board) -> int:
    w, b = board.occupied_co[chess.WHITE], board.occupied_co[chess.BLACK]
    return sum(_score(i // 7 + 1, chess.popcount(w & r), chess.popcount(b & r)) for i, r in enumerate(_REGIONS))


def divide_boards(boards: Iterable[chess.Board]) -> Division:
    """(middlegame start, endgame start) as indexes into `boards`; None where there is none."""
    mid = end = None
    for i, board in enumerate(boards):
        n = _majors_and_minors(board)
        if mid is None and (n <= 10 or _backrank_sparse(board) or _mixedness(board) > 150):
            mid = i
        if end is None and n <= 6:
            end = i
        if mid is not None and end is not None:
            break
    if mid is None:
        return None, None
    return (mid if end is None or mid < end else None), end


def divide(moves: list[str]) -> Division:
    """The division of a game (UCI moves from the start position); ply i is the position before move i."""

    def boards() -> Iterable[chess.Board]:
        b = chess.Board()
        yield b.copy(stack=False)
        for u in moves:
            b.push_uci(u)
            yield b.copy(stack=False)

    return divide_boards(boards())


def phase_at(division: Division, ply: int) -> str:
    """The phase of the position before move `ply`."""
    mid, end = division
    if end is not None and ply >= end:
        return ENDGAME
    if mid is not None and ply >= mid:
        return MIDDLEGAME
    return OPENING

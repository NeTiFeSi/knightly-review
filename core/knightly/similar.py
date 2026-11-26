"""Very similar puzzle positions: same side to move, and the piece maps differ on at most a few squares.

A new puzzle that is similar to one already made is not a note of its own. Instead each correct
answer of the old puzzle gets a comment saying whether it would still be right in the new position,
naming what differs ("… if the white pawn on h3 was on h4").
"""

from __future__ import annotations

from typing import Optional

import chess

MAX_SQUARES = 2  # how many squares may differ


def diff(a: chess.Board, b: chess.Board) -> list[tuple[chess.Square, Optional[chess.Piece], Optional[chess.Piece]]]:
    """The squares where the two boards differ: (square, piece in a, piece in b)."""
    pa, pb = a.piece_map(), b.piece_map()
    return [(sq, pa.get(sq), pb.get(sq)) for sq in sorted(set(pa) | set(pb)) if pa.get(sq) != pb.get(sq)]


def is_similar(a: chess.Board, b: chess.Board, max_squares: int = MAX_SQUARES) -> bool:
    """Same side to move, and the piece maps differ on at most `max_squares` squares (0 too: the same
    position reached at another ply)."""
    return a.turn == b.turn and len(diff(a, b)) <= max_squares


def _name(p: chess.Piece) -> str:
    return f"{chess.COLOR_NAMES[p.color]} {chess.piece_name(p.piece_type)}"


def describe(a: chess.Board, b: chess.Board) -> str:
    """What `b` has that `a` does not, as the tail of 'if …': 'the white pawn on h3 was on h4'."""
    d = diff(a, b)
    if not d:
        return "the same position had come up at a different move"
    if len(d) == 2:
        (s1, a1, b1), (s2, a2, b2) = d
        for (x, ax, bx), (y, ay, by) in (((s1, a1, b1), (s2, a2, b2)), ((s2, a2, b2), (s1, a1, b1))):
            if ax is not None and bx is None and ay is None and by == ax:  # one piece moved from x to y
                return f"the {_name(ax)} on {chess.square_name(x)} was on {chess.square_name(y)}"
    parts = []
    for sq, pa, pb in d:
        name = chess.square_name(sq)
        if pa is None and pb is not None:
            parts.append(f"there was a {_name(pb)} on {name}")
        elif pb is None and pa is not None:
            parts.append(f"the {_name(pa)} on {name} was gone")
        elif pa is not None and pb is not None:
            parts.append(f"the {_name(pa)} on {name} was a {_name(pb)}")
    return " and ".join(parts)


def comment(san: str, good_here: bool, good_there: bool, why: str) -> str:
    """The note for one answer of the old puzzle, which was `good_here` there and is `good_there` in the new one."""
    if good_here and good_there:
        return f"{san} would also be a good move if {why}."
    if good_here:
        return f"{san} would not be a good move if {why}."
    if good_there:
        return f"{san} is not a good move, but it would be if {why}."
    return f"{san} would still be a bad move if {why}."

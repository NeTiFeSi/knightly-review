"""Tactical motifs of a puzzle, found from the best move and the engine's line.

Names follow the themes of Lichess puzzles. The detectors are written here from the
definitions (they are not a port of lichess-puzzler, which is AGPL), so they are
simpler: they look at the position after the best move, and at the line that follows.

`detect` returns the motifs most important first; the first is the card's main motif.
"""

from __future__ import annotations

from typing import Optional

import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
MAX_MATE = 5
OTHER = "other"

# Most important first; also the order `detect` returns them in. "mate-in-N" goes where "mate" is.
PRIORITY = ["back-rank-mate", "mate", "promotion", "fork", "skewer", "pin", "discovered-attack",
            "hanging-piece", "sacrifice", "quiet-move"]


def _value(board: chess.Board, sq: int) -> int:
    p = board.piece_at(sq)
    return VALUES[p.piece_type] if p else 0


def _defended(board: chess.Board, sq: int, by: chess.Color) -> bool:
    return bool(board.attackers(by, sq))


def _back_rank_mate(board: chess.Board) -> bool:
    """`board` is checkmate with the king on its back rank, boxed in by its own pieces."""
    if not board.is_checkmate():
        return False
    king = board.king(board.turn)
    assert king is not None
    if chess.square_rank(king) != (0 if board.turn == chess.WHITE else 7):
        return False
    checkers = list(board.checkers())
    if not all(chess.square_rank(c) == chess.square_rank(king) for c in checkers):
        return False
    # Every flight square off the back rank is taken by the king's own pieces.
    step = 1 if board.turn == chess.WHITE else -1
    for df in (-1, 0, 1):
        f = chess.square_file(king) + df
        if 0 <= f < 8:
            sq = chess.square(f, chess.square_rank(king) + step)
            p = board.piece_at(sq)
            if p is None or p.color != board.turn:
                return False
    return True


def _fork(after: chess.Board, move: chess.Move, mover: chess.Color) -> bool:
    """The moved piece attacks two or more pieces worth winning, and can't be taken for free."""
    piece = after.piece_at(move.to_square)
    if piece is None:
        return False
    mine = VALUES[piece.piece_type]
    if piece.piece_type == chess.KING:
        return False
    targets = 0
    for sq in after.attacks(move.to_square):
        p = after.piece_at(sq)
        if p is None or p.color == mover:
            continue
        if VALUES[p.piece_type] > mine or not _defended(after, sq, not mover):
            if p.piece_type != chess.PAWN or piece.piece_type == chess.PAWN or VALUES[p.piece_type] > mine:
                targets += 1
    if targets < 2:
        return False
    # Not simply lost: nothing cheaper attacks it, or it is defended.
    cheaper = [a for a in after.attackers(not mover, move.to_square)
               if VALUES[after.piece_at(a).piece_type] < mine]  # type: ignore[union-attr]
    return not cheaper and (not after.attackers(not mover, move.to_square)
                            or _defended(after, move.to_square, mover))


def _pin(before: chess.Board, after: chess.Board, mover: chess.Color) -> bool:
    """The move pins an enemy piece to its king (a pin that wasn't there before)."""
    enemy = not mover
    return any(after.is_pinned(enemy, sq) and not before.is_pinned(enemy, sq)
               for sq in chess.scan_forward(after.occupied_co[enemy]))


def _skewer(after: chess.Board, move: chess.Move, mover: chess.Color) -> bool:
    """The moved slider attacks an enemy piece with a less valuable one behind it on the same line."""
    piece = after.piece_at(move.to_square)
    if piece is None or piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        return False
    for front in after.attacks(move.to_square):
        p = after.piece_at(front)
        if p is None or p.color == mover:
            continue
        # Walk on past `front`, along the same direction.
        df = chess.square_file(front) - chess.square_file(move.to_square)
        dr = chess.square_rank(front) - chess.square_rank(move.to_square)
        step_f = (df > 0) - (df < 0)
        step_r = (dr > 0) - (dr < 0)
        f, r = chess.square_file(front) + step_f, chess.square_rank(front) + step_r
        while 0 <= f < 8 and 0 <= r < 8:
            sq = chess.square(f, r)
            behind = after.piece_at(sq)
            if behind is not None:
                if behind.color != mover and VALUES[p.piece_type] > VALUES[behind.piece_type]:
                    return True
                break
            f, r = f + step_f, r + step_r
    return False


def _discovered(before: chess.Board, after: chess.Board, move: chess.Move, mover: chess.Color) -> bool:
    """Moving the piece opens a line for another of ours onto something it didn't attack before."""
    for sq in chess.scan_forward(after.occupied_co[mover]):
        p = after.piece_at(sq)
        if sq == move.to_square or p is None or p.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue
        newly = int(after.attacks(sq)) & ~int(before.attacks(sq)) & after.occupied_co[not mover]
        for t in chess.scan_forward(newly):
            if chess.between(sq, t) & chess.BB_SQUARES[move.from_square]:
                if after.is_check() and t == after.king(not mover) or VALUES[after.piece_at(t).piece_type] >= 3:  # type: ignore[union-attr]
                    return True
    return False


def _hanging(before: chess.Board, move: chess.Move, mover: chess.Color) -> bool:
    """The move takes a piece that nothing defended."""
    if not before.is_capture(move) or before.is_en_passant(move):
        return False
    target = before.piece_at(move.to_square)
    if target is None or target.piece_type == chess.PAWN:
        return False
    return not _defended(before, move.to_square, not mover)


def detect(board: chess.Board, line: list[str], mate: Optional[int] = None, sacrifice: bool = False) -> list[str]:
    """Motifs of the puzzle at `board` (before the best move), most important first.

    `line` is the engine's line from the best move on (UCI). `mate` is the forced mate
    in this many moves for the side to move (None if there is none). `sacrifice`: the
    best move gives up material for a better position (graded in select.py).
    """
    if not line:
        return [OTHER]
    mover = board.turn
    try:
        move = chess.Move.from_uci(line[0])
    except ValueError:
        return [OTHER]
    if move not in board.legal_moves:
        return [OTHER]
    found: set[str] = set()
    after = board.copy(stack=False)
    after.push(move)

    # The line, played out: a mate or a promotion by the mover.
    b = board.copy(stack=False)
    mating = False
    for i, u in enumerate(line):
        mv = chess.Move.from_uci(u)
        if mv not in b.legal_moves:
            break
        if i % 2 == 0 and mv.promotion:
            found.add("promotion")
        b.push(mv)
        if b.is_checkmate():
            mating = b.turn != mover
            if mating and _back_rank_mate(b):
                found.add("back-rank-mate")
            break
    if mate is not None and 0 < mate <= MAX_MATE:
        found.add(f"mate-in-{mate}")
    elif mating and (len(line) + 1) // 2 <= MAX_MATE:
        found.add(f"mate-in-{(len(line) + 1) // 2}")

    if _fork(after, move, mover):
        found.add("fork")
    if _skewer(after, move, mover):
        found.add("skewer")
    if _pin(board, after, mover):
        found.add("pin")
    if _discovered(board, after, move, mover):
        found.add("discovered-attack")
    if _hanging(board, move, mover):
        found.add("hanging-piece")
    if sacrifice:
        found.add("sacrifice")
    if not found and not board.is_capture(move) and not after.is_check():
        found.add("quiet-move")

    def rank(m: str) -> int:
        name = "mate" if m.startswith("mate-in-") else m
        return PRIORITY.index(name)

    out = sorted(found, key=lambda m: (rank(m), m))
    return out or [OTHER]

"""The solution tree of a puzzle: the good moves of both sides, followed until a stop.

From a puzzle's answers (`CardSpec.alternatives` accepted as good, and for the punishment puzzle also
its main move), each line is followed position by position, every position analysed in full
(`select.grade_position`):

- **Good moves** are the ones within `FOLLOW_GOOD` (1 %, fixed, not the user's `good_move`) of the best.
  A mate puzzle keeps the shortest mate for the solver and the longest resistance for the defender.
- **The defender** (the side not solving) always branches on all its good replies.
- **The solver's positions** are followed only when the gap between the good moves and the best other
  move is at least `GAP` (5 %); a smaller gap ends the line. In a mate puzzle the line ends when another,
  slower mate is also available, or when the solver is being mated.
- A line also ends at: game over; a draw by repetition, the fifty-move rule or lack of material; a
  decided game (no move can change the result, see `select.decided`); in a puzzle that is not a mate, a
  position where Stockfish sees a forced mate; a position already written in this note (it is a leaf
  that says it transposes; the card jumps to the position's continuation); `SAFETY_PLIES` plies.
- At the solver's positions every legal move is written (the good ones followed, the others as one-ply
  graded variations) so a wrong move gets graded.

The line ends on the move that led to the stop: the position of the stop is not written.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional

import chess

from .cache import fen_key
from .config import Config
from .model import CardSpec, Choice
from .progress import Tracker
from .select import ACCEPTED, Graded, _choices, decided, decided_text, grade_position

FOLLOW_GOOD = 1.0  # a move this close to the best (win-rate points) is followed; fixed on purpose
GAP = 5.0  # the solver's line goes on when the best move outside the good ones loses at least this much
SAFETY_PLIES = 80  # a line never goes on longer than this (it would take a pathological position)


def draw_reason(board: chess.Board) -> str:
    """Why the game is drawn on `board` (with its move history), or ''."""
    if board.is_stalemate():
        return "Stalemate."
    if board.is_repetition(3):
        return "Draw by threefold repetition."
    if board.is_fifty_moves():
        return "Draw by the fifty-move rule."
    if board.is_insufficient_material():
        return "Draw: not enough material."
    return ""


@dataclass
class _Ctx:
    an: object
    cfg: Config  # with good_move = FOLLOW_GOOD
    solver: chess.Color
    mate: bool  # a mate puzzle: Stockfish alone, followed to the end
    seen: set[str]  # positions written in this note
    quick_depth: int
    start: int  # length of the move stack at the puzzle position
    tracker: Optional[Tracker] = None
    nodes: int = 0  # positions analysed so far in the note's trees


def _join(a: str, b: str) -> str:
    return f"{a} {b}".strip()


def _good_set(graded: list[Graded]) -> tuple[list[Graded], str]:
    """The followed moves of a position and what kind of position it is: 'mating' (the side to move mates),
    'mated' (it is being mated) or 'plain'."""
    mating = [g for g in graded if g.mate is not None and g.mate > 0]
    if mating:
        shortest = min(g.mate for g in mating)  # type: ignore[type-var]
        return [g for g in mating if g.mate == shortest], "mating"
    if graded and all(g.mate is not None and g.mate < 0 for g in graded):
        longest = min(g.mate for g in graded)  # type: ignore[type-var]
        return [g for g in graded if g.mate == longest], "mated"
    return [g for g in graded if g.loss <= FOLLOW_GOOD], "plain"


def _expand(board: chess.Board, ch: Choice, ctx: _Ctx) -> None:
    """`board` is the position after `ch.move`: write what follows, or why the line ends."""
    if board.is_checkmate():
        return
    reason = draw_reason(board)
    if reason:
        ch.comment = _join(ch.comment, reason)
        return
    key = fen_key(board.fen())
    if key in ctx.seen:
        ch.comment = _join(ch.comment, "Transposes to a line above.")
        return
    if len(board.move_stack) - ctx.start >= SAFETY_PLIES:
        return
    if not ctx.mate:  # a quick Stockfish look: a forced mate ends the line (Lc0 never runs on it)
        s, _ = ctx.an.score(board, ctx.quick_depth)
        if s.mate is not None:
            ch.comment = _join(ch.comment, f"Mate in {abs(s.mate)} according to Stockfish.")
            return
    tr = ctx.tracker
    if tr:
        ctx.nodes += 1
        tr.plan(1)
        tr.message(f"Solution tree: {ctx.nodes} positions so far", step=f"ply {board.ply()}", force=False)
    graded, by_move = grade_position(board, ctx.an, ctx.cfg, None, board.ply(), sf_only=ctx.mate)
    if tr:
        tr.advance()
    if not graded:
        return
    if not ctx.mate:
        verdict = decided(board, by_move)
        if verdict is not None:
            ch.comment = _join(ch.comment, decided_text(board, verdict))
            return
    good, kind = _good_set(graded)
    solver_to_move = board.turn == ctx.solver
    if solver_to_move:
        rest = [g for g in graded if g not in good]
        if kind == "mated":
            return  # the solver is being mated
        if kind == "mating":
            if any(g.mate is not None and g.mate > 0 for g in rest):
                return  # other mates are available too: many ways to win
        elif not rest or min(g.loss for g in rest) < GAP:
            return
    ctx.seen.add(key)
    _, choices = _choices(graded, by_move, ctx.cfg, None)  # best first
    names = {g.move for g in good}
    for c in choices:
        if c.move in names:
            ch.children.append(c)
        elif solver_to_move:
            ch.children.append(c)  # a one-ply graded variation
    for c in ch.children:
        if c.move in names:
            board.push(chess.Move.from_uci(c.move))
            try:
                _expand(board, c, ctx)
            finally:
                board.pop()


def follow_puzzle(board: chess.Board, card: CardSpec, an, cfg: Config, *, solver: chess.Color, seen: set[str],
                  quick_depth: int, include_played: bool = False,
                  tracker: Optional[Tracker] = None) -> None:
    """Grow the solution tree under the answers of `card`, a puzzle at `board` (which carries the moves that
    led to it, for the repetition rules). `solver` is the colour finding the answer; `seen` the positions
    already written in the note (shared by its two puzzles). `include_played`: the card's main move is an
    answer too (the punishment puzzle)."""
    ctx = _Ctx(an=an, cfg=dataclasses.replace(cfg, good_move=FOLLOW_GOOD), solver=solver, mate=card.mate,
               seen=seen, quick_depth=quick_depth, start=len(board.move_stack), tracker=tracker)
    answers = ([card.played] if include_played else []) + [a for a in card.alternatives if a.nag in ACCEPTED]
    for ch in answers:
        board.push(chess.Move.from_uci(ch.move))
        try:
            _expand(board, ch, ctx)
        finally:
            board.pop()

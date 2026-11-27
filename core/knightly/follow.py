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
from dataclasses import dataclass, field

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
class Decision:
    """A position of the solver where the tree continues: a card of its own (see pipeline.py)."""

    before_fen: str  # the position before the opponent's last move
    reply: Choice  # that move, as written in the tree
    fen: str  # the decision position (full FEN)
    ply: int  # half-moves played there
    choices: list[Choice]  # the graded moves of the position, best first (the good ones carry the tree below them)
    good: list[str]  # the followed (good) moves, UCI
    depth: int  # plies from the puzzle's first position
    solver: chess.Color


@dataclass
class _Ctx:
    an: object
    cfg: Config  # with good_move = FOLLOW_GOOD
    solver: chess.Color
    mate: bool  # a mate puzzle: Stockfish alone, followed to the end
    seen: set[str]  # positions written in this note
    start: int  # length of the move stack at the puzzle position
    tracker: Tracker | None = None
    nodes: int = 0  # positions analysed so far in the note's trees
    max_plies: int = 0  # >0: stop after this many plies from the answer (the answer and one reply)
    decisions: list[Decision] = field(default_factory=list)


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


def _expand(board: chess.Board, ch: Choice, ctx: _Ctx, before_fen: str = "") -> None:
    """`board` is the position after `ch.move` (played from `before_fen`): write what follows, or why the line
    ends."""
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
    tr = ctx.tracker
    if tr:
        ctx.nodes += 1
        tr.plan(1)
        tr.message(f"Solution tree: {ctx.nodes} positions so far", step=f"ply {board.ply()}", force=False)
    graded, by_move = grade_position(board, ctx.an, ctx.cfg)
    if tr:
        tr.advance()
    if not graded:
        return
    best_mate = next(iter(by_move.values())).score.mate if by_move else None
    if not ctx.mate and best_mate is not None:  # in a puzzle that is not a mate, a forced mate ends the line
        ch.comment = _join(ch.comment, f"Mate in {abs(best_mate)} according to Stockfish.")
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
    if solver_to_move and before_fen:
        ctx.decisions.append(
            Decision(
                before_fen=before_fen,
                reply=ch,
                fen=board.fen(),
                ply=board.ply(),
                choices=ch.children,
                good=[c.move for c in ch.children if c.move in names],
                depth=len(board.move_stack) - ctx.start,
                solver=ctx.solver,
            )
        )
    if ctx.max_plies and len(board.move_stack) - ctx.start >= ctx.max_plies - 1:
        return  # the answer and one reply are enough: the replies are not analysed
    for c in ch.children:
        if c.move in names:
            here = board.fen()
            board.push(chess.Move.from_uci(c.move))
            try:
                _expand(board, c, ctx, here)
            finally:
                board.pop()


def follow_puzzle(
    board: chess.Board,
    card: CardSpec,
    an,
    cfg: Config,
    *,
    solver: chess.Color,
    seen: set[str],
    include_played: bool = False,
    tracker: Tracker | None = None,
    max_plies: int = 0,
) -> list[Decision]:
    """Grow the solution tree under the answers of `card`, a puzzle at `board` (which carries the moves that
    led to it, for the repetition rules). `solver` is the colour finding the answer; `seen` the positions
    already written in the note (shared by its two puzzles). `include_played`: the card's main move is an
    answer too (the punishment puzzle). `max_plies=2`: only each answer and the opponent's reply (their
    positions are not analysed further). Returns the decision positions found (see `Decision`)."""
    ctx = _Ctx(
        an=an,
        cfg=dataclasses.replace(cfg, annotate=dataclasses.replace(cfg.annotate, good_move=FOLLOW_GOOD)),
        solver=solver,
        mate=card.mate,
        seen=seen,
        start=len(board.move_stack),
        tracker=tracker,
        max_plies=max_plies,
    )
    answers = ([card.played] if include_played else []) + [a for a in card.alternatives if a.nag in ACCEPTED]
    for ch in answers:
        here = board.fen()
        board.push(chess.Move.from_uci(ch.move))
        try:
            _expand(board, ch, ctx, here)
        finally:
            board.pop()
    return ctx.decisions


def trimmed(ch: Choice, levels: int = 1) -> Choice:
    """A copy of `ch` that keeps `levels` of the moves below it (1: the opponent's reply). The "transposes"
    comments go: the card does not follow lines any more."""
    comment = ch.comment.replace("Transposes to a line above.", "").strip()
    return Choice(
        move=ch.move,
        nag=ch.nag,
        score=ch.score,
        comment=comment,
        tag=ch.tag,
        children=[trimmed(c, levels - 1) for c in ch.children] if levels > 0 else [],
    )

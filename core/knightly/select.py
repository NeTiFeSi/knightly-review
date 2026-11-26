"""Whether a position is a puzzle, and how every candidate move on its card is graded.

All judgements use the engine's own win/draw/loss estimate (WDL), as expected
score in percent: wins + draws / 2. A move's *loss* is how far its expected
score falls below the best move's.

Grading (NAGs) of the candidate moves at a puzzle position:

- A **good move** loses at most `cfg.good_move` points. A move that loses
  more but less than `cfg.inaccuracy` is normal play: no NAG (0). It isn't an
  accepted answer by default; played at a puzzle position it still makes the puzzle (it lost
  more than `cfg.min_loss`).
- A **sacrifice** gives up material in the short term while an alternative
  keeps more: `cfg.sacrifice_plies` plies down the engine's line (followed
  further while captures or checks are still going on, or a recapture that was
  available right after a capture is still owed past an in-between move), the side that moved has
  at least `cfg.sacrifice_points` of material (1/3/3/5/9) less than it has now,
  and at least that much less than the analysed alternative that keeps the most.
- In a **forced mate** (the best move mates by force), only moves that keep a
  forced mate count as good; a move that gives it up is ?! at best, even when
  the engine still calls the position won. How fast the mate is doesn't matter
  here (the card's `strictMate` setting asks for the fastest).

| NAG | When |
|---|---|
| !! ($3) | the only good move (with more than one legal move), and a sacrifice |
| □  ($8) | the only good move, not a sacrifice (a "singular move") |
| !? ($5) | a good move and a sacrifice, while another good move keeps more material |
| !  ($1) | any other good move |
| (none)  | loses more than a good move but less than `cfg.inaccuracy` (5) points |
| ?! ($6) | loses `cfg.inaccuracy` to 10 points |
| ?  ($2) | loses 10 to 20 points |
| ?? ($4) | loses more than 20 points |

!!, □, !? and ! count as correct answers on the card (its default acceptNags).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import chess

from . import motifs as motifs_mod
from .cache import fen_key
from .config import Config
from .model import CardSpec, Choice, Game, Line

NEUTRAL = 0  # no NAG
GOOD, MISTAKE, BRILLIANT, BLUNDER, INTERESTING, DUBIOUS, SINGULAR = 1, 2, 3, 4, 5, 6, 8
ACCEPTED = (BRILLIANT, SINGULAR, INTERESTING, GOOD)
NAG_SYMBOL = {1: "!", 2: "?", 3: "!!", 4: "??", 5: "!?", 6: "?!", 8: "□"}

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
EXTRA_PLIES = 6  # how far past `sacrifice_plies` a line is followed to let captures and checks finish


def material(board: chess.Board, color: chess.Color) -> int:
    """Material balance (1/3/3/5/9) from `color`'s side."""
    return sum(v * (len(board.pieces(pt, color)) - len(board.pieces(pt, not color))) for pt, v in VALUES.items())


def _capture_gain(board: chess.Board, sq: chess.Square) -> Optional[int]:
    """Net material the side to move wins by capturing on `sq` with its cheapest piece, then
    letting the exchange there run (either side may stop); None when it has no capture there."""
    caps = [m for m in board.legal_moves if m.to_square == sq and board.is_capture(m)]
    if not caps:
        return None
    m = min(caps, key=lambda c: VALUES.get(board.piece_type_at(c.from_square), 100))  # the king last
    got = VALUES.get(board.piece_type_at(sq), 0)
    board.push(m)
    back = _capture_gain(board, sq)
    board.pop()
    return got - max(0, back or 0)


def _recapture_owed(board: chess.Board, sq: chess.Square) -> bool:
    """Right after a capture on `sq`: can the side to move recapture there without losing material?"""
    gain = _capture_gain(board, sq)
    return gain is not None and gain >= 0


def _settled(board: chess.Board, last: chess.Move, after_board_capture: bool, nxt: Optional[chess.Move],
             owed: bool = False) -> bool:
    """No exchange or check in progress: counting material here doesn't cut a trade in half.

    `owed`: a recapture that was available right after the last capture on its square hasn't
    been played yet (an in-between move came first), so the trade isn't over.
    """
    if after_board_capture or owed or last.promotion or board.is_check():
        return False
    return nxt is None or not board.is_capture(nxt)


def line_material(board: chess.Board, moves: list[str], plies: int, analyser=None) -> int:
    """Material for the side to move in `board`, after playing `moves` (the candidate move first).

    Counted after `plies` plies, or later while captures or checks are still going
    on. When the line is too short, the engine continues it (if `analyser` is given).
    """
    color = board.turn
    b = board.copy(stack=False)
    seq = list(moves)
    done = 0
    last: Optional[chess.Move] = None
    captured = False
    owed: Optional[tuple[chess.Square, Optional[chess.Piece]]] = None  # square and capturer of a trade still to be finished
    while done < plies + EXTRA_PLIES and not b.is_game_over():
        if done >= len(seq) and analyser is not None and (done < plies or (last and not _settled(b, last, captured, None, owed is not None))):
            ev = analyser.evaluate(b, multipv=1)
            seq += ev.lines[0].moves if ev.lines else []
        if done >= len(seq):
            break
        mv = chess.Move.from_uci(seq[done])
        if mv not in b.legal_moves:
            break
        if done >= plies and last is not None and _settled(b, last, captured, mv, owed is not None):
            break
        captured = b.is_capture(mv)
        if owed is not None and (captured and mv.to_square == owed[0]):
            owed = None
        b.push(mv)
        last = mv
        done += 1
        if owed is not None and b.piece_at(owed[0]) != owed[1]:
            owed = None  # the capturer left or was taken: nothing left to recapture
        if captured and owed is None and _recapture_owed(b, mv.to_square):
            owed = (mv.to_square, b.piece_at(mv.to_square))
    return material(b, color)


@dataclass
class Graded:
    move: str
    loss: float  # expected-score points below the best move
    material: int  # short-term material balance along the line
    nag: int = 0
    mate: Optional[int] = None  # forced mate in this many moves, from the mover's side (negative: getting mated)


def grade(moves: list[Graded], legal_moves: int, cfg: Config, current: int = 0) -> list[Graded]:
    """Set each move's NAG (see the module docstring).

    `moves` are the analysed lines of one position; `current` is the material balance
    there now, from the side to move.
    """
    if not moves:
        return moves
    most = max(m.material for m in moves)
    mating = any(m.mate is not None and m.mate > 0 for m in moves)

    def is_good(m: Graded) -> bool:
        if mating:  # a forced mate on the board: only moves that keep one
            return m.mate is not None and m.mate > 0
        return m.loss <= cfg.good_move

    good = [m for m in moves if is_good(m)]
    for m in moves:
        if not is_good(m):
            if m.loss <= 10:
                m.nag = DUBIOUS if m.loss >= cfg.inaccuracy or mating else NEUTRAL
            else:
                m.nag = MISTAKE if m.loss <= 20 else BLUNDER
            continue
        sacrifice = m.material <= min(current, most) - cfg.sacrifice_points
        if len(good) == 1 and legal_moves > 1:
            m.nag = BRILLIANT if sacrifice else SINGULAR
        elif sacrifice and any(o.material > m.material for o in good if o is not m):
            m.nag = INTERESTING
        else:
            m.nag = GOOD
    return moves


def quick_nag(loss: float, inaccuracy: float) -> int:
    """NAG of a move from how much win rate it lost against the position before it (no alternatives needed).

    Only the bad grades can be told this way (?!, ?, ??, as in `grade`); good moves (!, !!, □, !?) need the
    other candidate moves analysed, so they only exist on puzzle cards.
    """
    if loss <= 10:
        return DUBIOUS if loss >= inaccuracy else NEUTRAL
    return MISTAKE if loss <= 20 else BLUNDER


GAME_LINES = 3  # enough to tell an only good move (the second line is worse) and the good moves apart


def game_nags(game: Game, analyser, cfg: Config, skip: Iterable[int] = (),
              progress: Optional[Callable[[int, int], None]] = None) -> dict[int, int]:
    """NAG of the played move at each ply of `game` that has one, graded like a puzzle position's moves.

    Plies in `skip` (the puzzle cards, graded in full there) and positions with a single legal move are left
    out. A plain good move (!) is not marked: nearly every move would be. !!, □ and !? and the bad grades are.
    Uses `GAME_LINES` lines per position (cached by the analyser), so it is cheaper than a puzzle's full look.
    `progress(done, total)` is called before each position.
    """
    skip = set(skip)
    out: dict[int, int] = {}
    board = chess.Board()
    for ply, u in enumerate(game.moves):
        if progress:
            progress(ply, len(game.moves))
        if ply not in skip and not board.is_game_over() and board.legal_moves.count() > 1:
            graded, _ = grade_position(board, analyser, cfg, u, ply, GAME_LINES)
            played = next((g for g in graded if g.move == u), None)
            if played is not None and played.nag not in (NEUTRAL, GOOD):
                out[ply] = played.nag
        board.push_uci(u)
    return out


def grade_position(board: chess.Board, analyser, cfg: Config, extra: Optional[str] = None,
                   ply: int = 99, lines: Optional[int] = None, sf_only: bool = False) -> tuple[list[Graded], dict[str, Line]]:
    """Deep multi-line analysis of `board`, graded. `extra` (e.g. the move played) is analysed too.

    `lines`: how many lines to analyse instead of the configured number. `sf_only`: Stockfish alone
    (the analyser's `evaluate` normally lets Lc0 judge, except in mates)."""
    multipv = lines or (cfg.deep_multipv if cfg.deep_multipv > 0 else board.legal_moves.count())  # 0 = every legal move
    ev = analyser.evaluate(board, multipv=multipv, ply=ply, **({"sf_only": True} if sf_only else {}))
    by_move = {l.moves[0]: l for l in ev.lines if l.moves}
    if not by_move:
        return [], {}
    if extra and extra not in by_move:
        after = board.copy(stack=False)
        after.push_uci(extra)
        pe = analyser.evaluate(after, ply=ply + 1)
        by_move[extra] = Line(pe.score, [extra] + (pe.lines[0].moves if pe.lines else []))
    white = board.turn == chess.WHITE
    best = max(l.score.win_pct(white) for l in by_move.values())
    graded = [
        Graded(m, max(0.0, best - l.score.win_pct(white)), line_material(board, l.moves, cfg.sacrifice_plies, analyser),
               mate=None if l.score.mate is None else (l.score.mate if white else -l.score.mate))
        for m, l in by_move.items()
    ]
    return grade(graded, board.legal_moves.count(), cfg, material(board, board.turn)), by_move


def decided(board: chess.Board, by_move: dict[str, Line]) -> Optional[bool]:
    """Whether no move can change the result for the side to move: True when even the worst move still
    scores above 50 % (it wins whatever it plays), False when even the best scores below 50 % (it loses
    whatever it plays), None otherwise. Positions with a forced mate in any line are never decided."""
    if not by_move or any(l.score.mate is not None for l in by_move.values()):
        return None
    white = board.turn == chess.WHITE
    pcts = [l.score.win_pct(white) for l in by_move.values()]
    if min(pcts) > 50:
        return True
    if max(pcts) < 50:
        return False
    return None


def decided_text(board: chess.Board, verdict: bool) -> str:
    """'The game is decided: White is winning.' for a `decided` verdict."""
    winner = board.turn if verdict else not board.turn
    return f"The game is decided: {chess.COLOR_NAMES[winner].capitalize()} is winning."


def _choices(graded: list[Graded], by_move: dict[str, Line], cfg: Config,
             played: Optional[str] = None) -> tuple[Optional[Choice], list[Choice]]:
    """The graded moves as the card's choices: (the played move's, the others best first)."""
    others: list[Choice] = []
    played_choice = None
    for g in sorted(graded, key=lambda g: g.loss):
        line = by_move[g.move]
        ch = Choice(move=g.move, nag=g.nag, score=line.score)
        if g.move == played:
            played_choice = ch
        else:
            others.append(ch)
    return played_choice, others


def build_card(board: chess.Board, ply: int, played: str, analyser, cfg: Config,
               opponent: str = "", min_loss: Optional[float] = None, tag_motifs: bool = True) -> Optional[CardSpec]:
    """Deep-analyse the puzzle position and grade its candidate moves.

    None when the position isn't a fair puzzle: only one legal move, or the deep look says the
    played move wasn't a mistake (it lost no more than `min_loss`, default `cfg.min_loss_at(ply)`,
    or it counts as a correct answer), or no other move is a correct answer, or no move can change the
    result (`decided`). `opponent` is the
    name of the player who made the move when it wasn't the user (an opponent-mistake card).
    `tag_motifs=False` skips the motif tagging (when only asking whether the position makes a card).
    """
    if board.legal_moves.count() < 2:
        return None
    graded, by_move = grade_position(board, analyser, cfg, played, ply)
    if not graded:
        return None
    if decided(board, by_move) is not None:  # no move can change the result: nothing to learn here
        return None
    g_played = next(g for g in graded if g.move == played)
    threshold = cfg.min_loss_at(ply) if min_loss is None else min_loss
    if g_played.loss <= threshold or g_played.nag in ACCEPTED:
        return None
    played_choice, choices = _choices(graded, by_move, cfg, played)
    assert played_choice is not None
    if not any(c.nag in ACCEPTED for c in choices):
        return None
    best_line = by_move[choices[0].move]
    sc = best_line.score
    cp = motifs_mod.MATE_CP * (1 if sc.mate > 0 else -1) if sc.mate is not None else (sc.cp or 0)
    if board.turn == chess.BLACK:
        cp = -cp
    found = [] if not tag_motifs else motifs_mod.detect(board, best_line.moves, cp, choices[0].nag in (BRILLIANT, INTERESTING))
    best_san = board.san(chess.Move.from_uci(choices[0].move))
    played_san = board.san(chess.Move.from_uci(played))
    if opponent:
        comment = f"{opponent} played {played_san} here, losing {g_played.loss:.0f}% win rate. {best_san} was best."
    else:
        comment = f"{played_san} loses {g_played.loss:.0f}% win rate. {best_san} was best."
    return CardSpec(
        fen_key=fen_key(board.fen()),
        ply=ply,
        played=played_choice,
        alternatives=choices,
        loss=g_played.loss,
        comment=comment,
        mine=not opponent,
        motifs=found,
        mate=choices[0].nag in ACCEPTED and _mates(graded, choices[0].move),
    )


def _mates(graded: list[Graded], move: str) -> bool:
    g = next((g for g in graded if g.move == move), None)
    return g is not None and g.mate is not None and g.mate > 0


def build_punish(board: chess.Board, ply: int, played_san: str, analyser, cfg: Config,
                 mine: bool = True) -> Optional[CardSpec]:
    """The second puzzle of a mistake: `board` is the position after the bad move (`ply` half-moves played),
    and the answer is the best reply. The card's main move is that reply, the other moves are graded as usual.

    None when the game is over there, the reply is forced (nothing to find), or the game is decided (no move
    can change the result): the note then has one card.
    """
    if board.is_game_over() or board.legal_moves.count() < 2:
        return None
    graded, by_move = grade_position(board, analyser, cfg, None, ply)
    if not graded or decided(board, by_move) is not None:  # no move can change the result: no second card
        return None
    best = min(graded, key=lambda g: g.loss).move
    main, others = _choices(graded, by_move, cfg, best)
    assert main is not None
    best_san = board.san(chess.Move.from_uci(best))
    return CardSpec(
        fen_key=fen_key(board.fen()),
        ply=ply,
        played=main,
        alternatives=others,
        loss=0.0,
        comment=f"{played_san} is punished by {best_san}.",
        mine=mine,
        mate=_mates(graded, best),
    )

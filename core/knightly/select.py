"""Which mistakes deserve a card, and how every candidate move on the card is graded.

All judgements use the engine's own win/draw/loss estimate (WDL), as expected
score in percent: wins + draws / 2. A move's *loss* is how far its expected
score falls below the best move's.

Grading (NAGs) of the candidate moves at a puzzle position:

- A **good move** loses at most `cfg.good_move` points. A move that loses
  more but less than `cfg.inaccuracy` is normal play: no NAG (0). It isn't an
  accepted answer by default, but it is never a mistake, so it makes no puzzle.
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
from typing import Iterable, Iterator, Optional

import chess

from . import motifs as motifs_mod, phases
from .cache import fen_key
from .config import Config
from .model import Candidate, CardSpec, Choice, Game, Line, Score
from .openings import Group

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


def game_nags(game: Game, analyser, cfg: Config, skip: Iterable[int] = ()) -> dict[int, int]:
    """NAG of the played move at each ply of `game` that has one, graded like a puzzle position's moves.

    Plies in `skip` (the puzzle cards, graded in full there) and positions with a single legal move are left
    out. A plain good move (!) is not marked: nearly every move would be. !!, □ and !? and the bad grades are.
    Uses `GAME_LINES` lines per position (cached by the analyser), so it is cheaper than a puzzle's full look.
    """
    skip = set(skip)
    out: dict[int, int] = {}
    board = chess.Board()
    for ply, u in enumerate(game.moves):
        if ply not in skip and not board.is_game_over() and board.legal_moves.count() > 1:
            graded, _ = grade_position(board, analyser, cfg, u, ply, GAME_LINES)
            played = next((g for g in graded if g.move == u), None)
            if played is not None and played.nag not in (NEUTRAL, GOOD):
                out[ply] = played.nag
        board.push_uci(u)
    return out


def move_losses(game: Game, scores: list[Optional[Score]], cfg: Config) -> Iterator[Candidate]:
    """Every scored move of `game` with its quick-pass loss, in ply order.

    Only the user's moves, unless `cfg.include_opponent`. `note` is left empty.
    """
    board = chess.Board()
    user = chess.WHITE if game.user_is_white else chess.BLACK
    division = phases.divide(game.moves)
    for ply, u in enumerate(game.moves):
        mine = board.turn == user
        if mine or cfg.include_opponent:
            before = scores[ply] if ply < len(scores) else None
            after = scores[ply + 1] if ply + 1 < len(scores) else None
            if before is not None and after is not None:
                white = board.turn == chess.WHITE
                yield Candidate(
                    game_key=game.key,
                    ply=ply,
                    fen_key=fen_key(board.fen()),
                    played=u,
                    loss=before.win_pct(white) - after.win_pct(white),
                    phase=phases.phase_at(division, ply),
                    mine=mine,
                )
        board.push_uci(u)


def find_candidates(
    analysed: Iterable[tuple[Game, list[Optional[Score]]]],
    cfg: Config,
    exclude: set[str] = frozenset(),
) -> list[Candidate]:
    """One note per game and phase: moves that lost at least `cfg.min_loss` points, game by game.

    Games newest first, each game's moves in order. A position is offered once (from
    the newest game that reached it) and never if it is in `exclude` (already on a card).
    """
    seen = set(exclude)
    out = []
    for game, scores in sorted(analysed, key=lambda gs: gs[0].end_ts, reverse=True):
        for c in move_losses(game, scores, cfg):
            if c.loss >= cfg.min_loss and c.fen_key not in seen:
                seen.add(c.fen_key)
                c.note = game_note_key(game.key, c.phase)
                out.append(c)
    return out


def ranked_candidates(
    analysed: Iterable[tuple[Game, list[Optional[Score]]]],
    cfg: Config,
    exclude: set[str] = frozenset(),
) -> list[Candidate]:
    """One note per game and phase, mistakes from all games ranked together (the original ordering).

    Moves losing at least `cfg.min_loss` points, one per position, best first by
    score = loss × weight × (1 + 0.5 × (recurrences − 1)), where weight is 1.0 for your
    moves in games you didn't win, 0.6 in games you won, and 0.8 for the opponent's
    mistakes (`cfg.include_opponent`); recurrences counts the games that reached the position.
    """
    seen_count: dict[str, int] = {}
    found: dict[str, Candidate] = {}
    for game, scores in analysed:
        my_weight = 1.0 if game.user_result() < 1 else 0.6  # mistakes that cost points matter most
        for c in move_losses(game, scores, cfg):
            seen_count[c.fen_key] = seen_count.get(c.fen_key, 0) + 1
            if c.loss >= cfg.min_loss and c.fen_key not in exclude:
                c.score = c.loss * (my_weight if c.mine else 0.8)
                c.note = game_note_key(game.key, c.phase)
                if c.fen_key not in found or c.score > found[c.fen_key].score:
                    found[c.fen_key] = c
    out = []
    for c in found.values():
        c.recurrences = seen_count.get(c.fen_key, 1)
        c.score *= 1 + 0.5 * (c.recurrences - 1)
        out.append(c)
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def game_note_key(game_key: str, phase: str) -> str:
    """'lichess:abcd1234' + 'endgame' → 'lichess:abcd1234:endgame'."""
    return f"{game_key}:{phase}"


def parse_game_note_key(key: str) -> Optional[tuple[str, str]]:
    """(game key, phase) of a game note key, or None for another key."""
    game_key, _, phase = key.rpartition(":")
    if phase not in phases.PHASES or ":" not in game_key or key.startswith("opening:"):
        return None
    return game_key, phase


MOTIF_NOTE_CARDS = 10  # a motif note holds this many cards; the next one starts a new note


def motif_note_key(motif: str, n: int) -> str:
    """'fork', 3 → 'motif:fork:3'."""
    return f"motif:{motif}:{n}"


def parse_motif_note_key(key: str) -> Optional[tuple[str, int]]:
    """(motif, number) of a motif note key, or None for another key."""
    parts = key.split(":")
    if len(parts) != 3 or parts[0] != "motif" or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


def opening_note_key(group: Group, ply: int) -> str:
    return f"opening:{group.color}:{group.slug}:{ply + 1}"


def parse_opening_note_key(key: str) -> Optional[tuple[str, str, int]]:
    """(colour, family slug, ply index) of an opening note key, or None for a game key."""
    parts = key.split(":")
    if len(parts) != 4 or parts[0] != "opening" or not parts[3].isdigit():
        return None
    return parts[1], parts[2], int(parts[3]) - 1


def opening_candidates(
    groups: list[Group],
    analysed: dict[str, list[Optional[Score]]],
    cfg: Config,
    exclude: set[str] = frozenset(),
) -> list[Candidate]:
    """One note per opening and ply: moves losing more than `cfg.opening_min_loss` points.

    Groups in the given order (most important first), then ply from the first move on,
    then the group's games newest first. `analysed` maps game key → quick-pass scores;
    games without them are skipped. A position is offered once, never if in `exclude`.
    """
    seen = set(exclude)
    out = []
    for grp in groups:
        by_ply: dict[int, list[Candidate]] = {}
        for g in grp.games:  # newest first
            scores = analysed.get(g.key)
            if scores is None:
                continue
            for c in move_losses(g, scores, cfg):
                if c.loss > cfg.opening_min_loss:
                    by_ply.setdefault(c.ply, []).append(c)
        for ply in sorted(by_ply):
            for c in by_ply[ply]:
                if c.fen_key not in seen:
                    seen.add(c.fen_key)
                    c.note = opening_note_key(grp, ply)
                    out.append(c)
    return out


def grade_position(board: chess.Board, analyser, cfg: Config, extra: Optional[str] = None,
                   ply: int = 99, lines: Optional[int] = None) -> tuple[list[Graded], dict[str, Line]]:
    """Deep multi-line analysis of `board`, graded. `extra` (e.g. the move played) is analysed too.

    `lines`: how many lines to analyse instead of the configured number."""
    multipv = lines or (cfg.deep_multipv if cfg.deep_multipv > 0 else board.legal_moves.count())  # 0 = every legal move
    ev = analyser.evaluate(board, multipv=multipv, ply=ply)
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


def build_card(board: chess.Board, ply: int, played: str, analyser, cfg: Config,
               opponent: str = "", min_loss: Optional[float] = None, tag_motifs: bool = True) -> Optional[CardSpec]:
    """Deep-analyse the puzzle position and grade its candidate moves.

    None when the position isn't a fair puzzle: only one legal move, or the deep
    look says the played move wasn't a real mistake (it lost less than half of
    `min_loss`, default `cfg.min_loss`, or it counts as a correct answer). `opponent`
    is the name of the player who made the move when it wasn't the user (an
    opponent-mistake card). `tag_motifs=False` skips the motif tagging (when only
    asking whether the position makes a card).
    """
    if board.legal_moves.count() < 2:
        return None
    graded, by_move = grade_position(board, analyser, cfg, played, ply)
    if not graded:
        return None
    g_played = next(g for g in graded if g.move == played)
    threshold = cfg.min_loss if min_loss is None else min_loss
    if g_played.loss < threshold / 2 or g_played.nag in ACCEPTED or g_played.nag == NEUTRAL:  # the quick pass overstated it
        return None
    choices: list[Choice] = []
    played_choice = None
    for g in sorted(graded, key=lambda g: g.loss):
        line = by_move[g.move]
        ch = Choice(move=g.move, nag=g.nag, score=line.score)
        if g.nag in ACCEPTED:
            ch.reply = line.moves[1 : cfg.line_plies]
        if g.move == played:
            played_choice = ch
        else:
            choices.append(ch)
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
    )

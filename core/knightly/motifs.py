"""Tactical motifs of a puzzle, found from the best move and the engine's line.

The tagging is lichess-puzzler's (`cook.py`, ported in cook.py and cookutil.py), so the names are
Lichess's puzzle themes, in kebab-case: `fork`, `back-rank-mate`, `mate-in-2`, `discovered-attack`…
It needs the opponent's last move before the puzzle position (Lichess puzzles start there), so
a puzzle at the first move of a game, or a line the tagger can't read, gets `other`.

`detect` returns the motifs most important first; the first is the card's main motif.
"""

from __future__ import annotations

import re
from typing import Optional

import chess
import chess.pgn

from . import cook

OTHER = "other"
SOLUTION_PLIES = 7  # the engine line is cut to this many plies (the solver's moves come first and last)

# Lichess tags that say how a puzzle looks or what the position is, not what the tactic is.
_NOT_MOTIFS = {"short", "long", "veryLong", "oneMove", "crushing", "advantage", "equality", "mate",
               "pawnEndgame", "knightEndgame", "bishopEndgame", "rookEndgame", "queenEndgame",
               "queenRookEndgame"}

# Most important first: specific mates, then the tactics that name the idea, then the general ones.
PRIORITY = [
    "smotheredMate", "backRankMate", "anastasiaMate", "hookMate", "arabianMate", "bodenMate",
    "doubleBishopMate", "dovetailMate", "mateIn1", "mateIn2", "mateIn3", "mateIn4", "mateIn5",
    "underPromotion", "promotion", "enPassant", "fork", "skewer", "discoveredAttack", "doubleCheck", "pin",
    "hangingPiece", "trappedPiece", "deflection", "attraction", "overloading", "interference", "intermezzo",
    "clearance", "xRayAttack", "capturingDefender", "exposedKing", "attackingF2F7", "collinearMove",
    "zugzwang", "advancedPawn", "sacrifice", "defensiveMove", "quietMove", "castling", "kingsideAttack",
    "queensideAttack", "coercion",
]


def slug(tag: str) -> str:
    """'backRankMate' → 'back-rank-mate'; 'mateIn2' → 'mate-in-2'."""
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", "-", tag).lower()


def _puzzle(board: chess.Board, line: list[str], cp: int) -> Optional[cook.Puzzle]:
    """The Lichess-style puzzle: the game before the opponent's last move, that move, then `line`."""
    if not board.move_stack:
        return None
    root = board.copy()
    previous = root.pop()
    game = chess.pgn.Game()
    game.setup(root)
    node = game.add_variation(previous)
    b = board.copy(stack=False)
    for i, u in enumerate(line):
        move = chess.Move.from_uci(u)
        if move not in b.legal_moves:
            break
        node = node.add_variation(move)
        b.push(move)
        if b.is_checkmate() or i + 1 >= SOLUTION_PLIES:
            break
    end = game.end()
    if len(list(game.mainline())) % 2 == 1 and end.parent is not None:  # ends on the opponent's move: drop it
        end.parent.variations.remove(end)
    if len(list(game.mainline())) < 2:
        return None
    return cook.Puzzle("kr", game, cp)


def detect(board: chess.Board, line: list[str], cp: int = 0, sacrifice: bool = False) -> list[str]:
    """Motifs of the puzzle at `board` (before the best move), most important first.

    `board` should carry its move stack (the opponent's last move). `line` is the engine's line from
    the best move on (UCI). `cp`: its score for the side to move, in centipawns (mate: ±10000).
    `sacrifice`: the best move gives up material (graded in select.py), which adds `sacrifice`.
    """
    tags: list[str] = []
    try:
        puzzle = _puzzle(board, line, cp)
        if puzzle is not None:
            tags = [t for t in cook.cook(puzzle) if t not in _NOT_MOTIFS]
    except Exception:  # the tagger meets positions Lichess puzzles never have
        tags = []
    if sacrifice and "sacrifice" not in tags:
        tags.append("sacrifice")
    rank = {t: i for i, t in enumerate(PRIORITY)}
    tags.sort(key=lambda t: (rank.get(t, len(rank)), t))
    return [slug(t) for t in tags] or [OTHER]

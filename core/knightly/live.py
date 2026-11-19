"""Answering a card's live-play questions (see card/src/engine.ts).

The card asks for the best lines of a position, or for specific moves only; the
answer uses the same engines and the same Stockfish/Lc0 rule as the analysis.
"""

from __future__ import annotations

from typing import Any, Optional

import chess

from .engine.analyse import Analyser


def query(analyser: Analyser, fen: str, multipv: int = 1, moves: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Lines for `fen` (best first), as the card's ELine: uci, pv, wdl, cp, mate; White's view."""
    board = chess.Board(fen)
    if moves:
        legal = {m.uci() for m in board.legal_moves}
        bad = [m for m in moves if m not in legal]
        if bad:
            raise ValueError(f"illegal move(s) {bad} in {fen}")
    ev = analyser.evaluate(board, multipv=max(1, int(multipv)), moves=list(moves) if moves else None)
    out = []
    for line in ev.lines:
        if not line.moves:
            continue
        s = line.score
        out.append({"uci": line.moves[0], "pv": line.moves, "wdl": list(s.wdl or (0, 1000, 0)),
                    "cp": s.cp, "mate": s.mate})
    return out

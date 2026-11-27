"""Name a game's opening (used for a puzzle's tags).

The table is Lichess's chess-openings (CC0), stored as the position each named line
reaches, so transpositions are recognised too. A game's opening is the last position
along its moves that the table names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import chess

from .cache import fen_key
from .model import Game

TABLE = Path(__file__).resolve().parent / "data" / "openings.tsv"
OTHER = "Other"
MAX_BOOK_PLIES = 40  # the table's longest lines are shorter than this


@lru_cache(maxsize=1)
def load() -> dict[str, tuple[str, str]]:
    """Position (fen_key) → (ECO, name)."""
    out: dict[str, tuple[str, str]] = {}
    with TABLE.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            eco, name, key = line.rstrip("\n").split("\t")
            out.setdefault(key, (eco, name))
    return out


def classify(moves: Iterable[str]) -> tuple[str, str]:
    """(ECO, name) of the last named position along `moves` (UCI); ("", "Other") if none."""
    table = load()
    found = ("", OTHER)
    b = chess.Board()
    for i, u in enumerate(moves):
        if i >= MAX_BOOK_PLIES:
            break
        b.push_uci(u)
        found = table.get(fen_key(b.fen()), found)
    return found


def family(name: str) -> str:
    """'Sicilian Defense: Najdorf Variation' → 'Sicilian Defense'."""
    return name.split(":", 1)[0].strip()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "other"


def color(game: Game) -> str:
    return "white" if game.user_is_white else "black"

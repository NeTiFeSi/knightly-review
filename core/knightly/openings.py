"""Name a game's opening, and rank openings by how many points they cost.

The table is Lichess's chess-openings (CC0), stored as the position each named line
reaches, so transpositions are recognised too. A game's opening is the last position
along its moves that the table names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import chess

from .cache import fen_key
from .model import Game

TABLE = Path(__file__).resolve().parent / "data" / "openings.tsv"
OTHER = "Other"
MAX_BOOK_PLIES = 40  # the table's longest lines are shorter than this
# Points per game for ranking: a loss weighs 4, a draw 2, a win 1.
RESULT_WEIGHT = {0.0: 4, 0.5: 2, 1.0: 1}


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


@dataclass
class Group:
    """The user's games in one opening family with one colour."""

    color: str  # "white" | "black"
    family: str
    games: list[Game] = field(default_factory=list)  # newest first
    importance: int = 0  # 4 per loss, 2 per draw, 1 per win

    @property
    def slug(self) -> str:
        return slug(self.family)

    @property
    def label(self) -> str:
        return f"{self.family} ({self.color.capitalize()})"


def rank(games: Iterable[Game]) -> list[Group]:
    """Groups by (colour, family), most important first; ties by number of games, then name."""
    groups: dict[tuple[str, str], Group] = {}
    for g in sorted(games, key=lambda g: g.end_ts, reverse=True):
        c, fam = color(g), family(classify(g.moves)[1])
        grp = groups.setdefault((c, fam), Group(c, fam))
        grp.games.append(g)
        grp.importance += RESULT_WEIGHT.get(g.user_result(), 2)
    return sorted(groups.values(), key=lambda gr: (-gr.importance, -len(gr.games), gr.family, gr.color))

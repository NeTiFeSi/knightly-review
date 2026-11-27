"""The puzzles made so far: each is one note with two puzzles (the mistake, and the punishment of it).

A puzzle's id is its position (`fen_key`) plus its ply, and its note key is derived from that. What
the run needs to know about a puzzle later (its tags, the answers' grades, the comments added when
similar positions turn up) is saved in the cache as one small JSON record per puzzle.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

import chess
import chess.pgn

from .cache import Cache, fen_key
from .select import ACCEPTED

KEY_PREFIX = "puzzle:"
INDEX = "puzzle-index"


def puzzle_id(fen: str, ply: int) -> str:
    """Id of a puzzle: its position and ply."""
    return f"{ply}|{fen_key(fen)}"


def puzzle_key(fen: str, ply: int) -> str:
    """Note key of a puzzle, 'puzzle:<ply>:<hash of the position>'."""
    return f"{KEY_PREFIX}{ply}:{hashlib.sha1(fen_key(fen).encode()).hexdigest()[:12]}"


def parse_key(key: str) -> tuple[int, str] | None:
    """(ply, position hash) of a puzzle note key, or None for another key."""
    parts = key.split(":")
    if len(parts) != 3 or parts[0] != "puzzle" or not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2]


@dataclass
class Puzzle:
    key: str
    fen: str  # the position of the first puzzle (the mistake)
    ply: int
    moves: dict[str, int] = field(default_factory=dict)  # UCI → NAG of the first puzzle's moves
    played: str = ""  # the mistake (UCI)
    after_fen: str = ""  # the position of the second puzzle
    after_ply: int = 0
    after_moves: dict[str, int] = field(default_factory=dict)  # UCI → NAG of the second card's moves
    head: str = ""  # first line of the note's Notes field (phase and game link)
    tags: list[str] = field(default_factory=list)
    pgn: str = ""
    cards: str = ""
    card_ids: dict[str, int] = field(default_factory=dict)
    links: dict[str, list[str]] = field(default_factory=dict)  # id of a similar position → comments on our answers
    notes: str = ""  # the Notes field as last written
    stored: bool = True  # False: rebuilt from a note's PGN only (tags and the rest unknown), not ours to rewrite

    @property
    def id(self) -> str:
        return puzzle_id(self.fen, self.ply)

    def good(self) -> list[str]:
        """The answers that count as correct."""
        return [m for m, nag in self.moves.items() if nag in ACCEPTED]


def from_pgn(key: str, pgn: str) -> Puzzle | None:
    """The first puzzle (cloze 1, or the lowest number) of a note's PGN, with its answers' NAGs."""
    from .notes import KR_TAG  # notes imports this module

    f = io.StringIO(pgn)
    best: tuple[int, chess.pgn.GameNode] | None = None
    while (g := chess.pgn.read_game(f)) is not None:
        for node in g.mainline():
            m = KR_TAG.search(node.comment or "")
            if m and (best is None or int(m.group(1)) < best[0]):
                best = (int(m.group(1)), node)
    if best is None or best[1].parent is None or best[1].move is None:
        return None
    node = best[1]
    parent = node.parent
    board = parent.board()
    moves = {v.move.uci(): next(iter(v.nags), 0) for v in parent.variations if v.move}
    return Puzzle(key=key, fen=board.fen(), ply=board.ply(), moves=moves, played=node.move.uci(), stored=False)


class Store:
    """The saved puzzle records, in the cache's small key-value state."""

    def __init__(self, cache: Cache):
        self.cache = cache

    def keys(self) -> list[str]:
        return list(self.cache.get(INDEX) or [])

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def get(self, key: str) -> Puzzle | None:
        d = self.cache.get(KEY_PREFIX + key)
        if not d:
            return None
        names = {f for f in Puzzle.__dataclass_fields__}
        return Puzzle(**{k: v for k, v in d.items() if k in names})

    def put(self, p: Puzzle) -> None:
        keys = self.keys()
        if p.key not in keys:
            self.cache.set(INDEX, keys + [p.key])
        d: dict[str, Any] = asdict(p)
        self.cache.set(KEY_PREFIX + p.key, d)

    def delete(self, key: str) -> None:
        self.cache.set(INDEX, [k for k in self.keys() if k != key])
        self.cache.set(KEY_PREFIX + key, None)

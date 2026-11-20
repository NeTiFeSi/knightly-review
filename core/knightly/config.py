"""Settings for an update run. Unknown keys are ignored, so the add-on can keep its own keys alongside."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any


def _half_cores() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


@dataclass
class Config:
    lichess_user: str = ""
    chesscom_user: str = ""
    lichess_token: str = ""
    # Engines: Stockfish is required; Lc0 is optional and, when on, runs alongside it.
    stockfish_path: str = ""
    sf_depth: int = 13  # fixed depth for every Stockfish analysis; 20+ is recommended if the CPU allows
    threads: int = 0  # Stockfish threads; 0 = half the CPU cores
    hash_mb: int = 64  # Stockfish hash table (RAM), in MB
    use_lc0: bool = False
    lc0_path: str = ""
    lc0_time: float = 0.5  # seconds per Lc0 analysis (Lc0 is searched by time, not depth)
    lc0_weights: str = ""  # network file; empty = Lc0 finds one itself
    deep_multipv: int = 0  # lines analysed at a puzzle position; 0 = every legal move
    # What to fetch and how much work one run does.
    history_days: int = 90
    time_classes: list[str] = field(default_factory=lambda: ["blitz", "rapid", "classical", "correspondence"])
    rated_only: bool = False
    max_games_per_run: int = 40
    max_new_cards: int = 10
    # How cards are grouped into notes and which come first:
    # "game": one note per game, games newest first, every move losing `min_loss` or more.
    # "opening": one note per opening family + your colour + ply. Openings that cost the most
    #   points (4 per loss, 2 per draw, 1 per win) first, then ply by ply from the first move,
    #   every move losing more than `opening_min_loss`.
    note_grouping: str = "game"
    opening_min_loss: float = 2.0  # opening mode: any move that isn't a "good move" (?!, ?, ??)
    # Judging moves. All percentages are points of the engine's expected score (WDL: wins + draws / 2).
    min_loss: float = 10.0  # a played move must lose at least this much to become a card
    include_opponent: bool = False  # also make cards from the opponent's mistakes (puzzle from their side)
    good_move: float = 2.0  # a "good move" is at most this far below the best move
    sacrifice_points: int = 2  # material (1/3/3/5/9) given up against the alternative that keeps the most
    sacrifice_plies: int = 4  # where along the engine's line material is counted (then captures/checks finish)
    line_plies: int = 2  # candidate move + opponent reply shown on the back

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Config":
        d = dict(d or {})
        if d.get("engine_path") and not d.get("stockfish_path"):  # settings from before Lc0 support
            d["stockfish_path"] = d["engine_path"]
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def engine_threads(self) -> int:
        return self.threads if self.threads > 0 else _half_cores()

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
    sf_depth: int = 13  # fixed depth for every Stockfish analysis
    threads: int = 0  # Stockfish threads; 0 = half the CPU cores
    hash_mb: int = 64  # Stockfish hash table (RAM), in MB
    use_lc0: bool = False
    lc0_path: str = ""
    lc0_weights: str = ""  # network file; empty = Lc0 finds one itself
    deep_multipv: int = 0  # lines analysed at a puzzle position; 0 = every legal move
    # What to fetch and how much work one run does.
    history_days: int = 90  # 0 or negative = all time
    time_classes: list[str] = field(default_factory=lambda: ["blitz", "rapid", "classical", "correspondence"])
    rated_only: bool = False
    analysis_minutes: float = 10  # engine time per update; 0 = until enough cards are found
    max_new_cards: int = 10
    # Keep analysing after an update has made its cards: "off"; "precalculate" (keep deepening and
    # confirming candidates and save them for the next update); "unlimited" (keep adding cards, in
    # batches of max_new_cards, with no per-update cap). `keep_running_hours`: 0 = until stopped or
    # everything is at full depth. Used by the add-on's runner, not by one run_update.
    keep_running: str = "off"
    keep_running_hours: float = 0
    # Judging moves. All percentages are points of the engine's expected score (WDL: wins + draws / 2).
    min_loss: float = 1.0  # a played move must lose more than this to become a puzzle (see `min_loss_at`)
    include_opponent: bool = False  # also make puzzles from the opponent's mistakes (after ours, at each ply)
    good_move: float = 1.0  # a "good move" is at most this far below the best move
    inaccuracy: float = 5.0  # a move losing this much or more is an inaccuracy (?!); between "good" and this is normal play: no NAG, no puzzle
    sacrifice_points: int = 2  # material (1/3/3/5/9) given up against the alternative that keeps the most
    sacrifice_plies: int = 4  # where along the engine's line material is counted (then captures/checks finish)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Config:
        d = dict(d or {})
        if d.get("engine_path") and not d.get("stockfish_path"):  # settings from before Lc0 support
            d["stockfish_path"] = d["engine_path"]
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def min_loss_at(self, ply: int) -> float:
        """How much win rate a move played at `ply` (half-moves already played) must lose to become a puzzle.

        One value for every ply for now; this is where per-ply bands (1 % up to ply 10, 5 % up to 30…) go."""
        return self.min_loss

    def engine_threads(self) -> int:
        return self.threads if self.threads > 0 else _half_cores()

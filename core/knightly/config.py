"""Settings, one section per stage. Unknown keys are ignored, so the add-on can keep its own keys alongside.

The stages: extract games → annotate moves → select puzzle candidates →
build puzzles → manage notes. Each stage reads its own section (and `engine`, the shared Stockfish), and each
section has its own fingerprint, so changing one stage's settings only redoes that stage's work.

`Config.from_dict` takes the sectioned form (`{"extract": {...}, "annotate": {...}}`) and also the flat form every
earlier version stored (`{"lichess_user": ..., "sf_depth": ...}`): each flat key goes to the section that owns it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any


def _half_cores() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


@dataclass
class Extract:
    """Where games come from and which ones are kept."""

    lichess_user: str = ""
    chesscom_user: str = ""
    lichess_token: str = ""  # a secret: never in a fingerprint or the add-on's config
    history_days: int = 90  # 0 or negative = all time
    time_classes: list[str] = field(default_factory=lambda: ["blitz", "rapid", "classical", "correspondence"])
    rated_only: bool = False


@dataclass
class Engine:
    """The shared Stockfish process: where it is and how much of the machine it may use."""

    stockfish_path: str = ""
    threads: int = 0  # Stockfish threads; 0 = half the CPU cores
    hash_mb: int = 64  # Stockfish hash table (RAM), in MB

    def engine_threads(self) -> int:
        return self.threads if self.threads > 0 else _half_cores()


@dataclass
class Annotate:
    """How moves are searched and judged.

    Two kinds of search, both at a fixed depth:
    - the scan (`scan_depth`): a cheap first look at each position with the fewest lines that can measure a
      loss: the best move (one line) and the moves the games played, each searched on its own;
    - the full search (`sf_depth`): one line per legal move (MultiPV = every move), so each move has an exact
      score from the same search. Only the positions the scan flags get it.
    All percentages are points of the engine's expected score (WDL: wins + draws / 2)."""

    sf_depth: int = 13
    scan_depth: int = 10
    good_move: float = 1.0  # a "good move" is at most this far below the best move
    inaccuracy: float = 5.0  # a move losing this much or more is an inaccuracy (?!); between "good" and this is normal play: no NAG, no puzzle
    sacrifice_points: int = 2  # material (1/3/3/5/9) given up against the alternative that keeps the most
    sacrifice_plies: int = 4  # where along the engine's line material is counted (then captures/checks finish)

    def scan_depth_used(self) -> int:
        """The scan's depth: `scan_depth`, but never deeper than the full search."""
        return max(1, min(self.scan_depth, self.sf_depth))


@dataclass
class Select:
    """Which annotated mistakes become puzzle candidates."""

    min_loss: float = 1.0  # a played move must lose more than this to become a puzzle (see `min_loss_at`)
    include_opponent: bool = False  # also make puzzles from the opponent's mistakes (after ours, at each ply)

    def min_loss_at(self, ply: int) -> float:
        """How much win rate a move played at `ply` (half-moves already played) must lose to become a puzzle.

        One value for every ply for now; this is where per-ply bands (1 % up to ply 10, 5 % up to 30…) go."""
        return self.min_loss


@dataclass
class Build:
    """How much one update makes, and for how long it runs."""

    analysis_minutes: float = 10  # engine time per update; 0 = until enough cards are found
    max_new_cards: int = 10
    # Keep analysing after an update has made its cards: "off"; "precalculate" (keep deepening and
    # confirming candidates and save them for the next update); "unlimited" (keep adding cards, in
    # batches of max_new_cards, with no per-update cap). `keep_running_hours`: 0 = until stopped or
    # everything is at full depth. Used by the add-on's runner, not by one run_update.
    keep_running: str = "off"
    keep_running_hours: float = 0


@dataclass
class Manage:
    """What is done to the notes already in the collection. Nothing is configurable yet."""


SECTIONS: dict[str, type] = {
    "extract": Extract,
    "engine": Engine,
    "annotate": Annotate,
    "select": Select,
    "build": Build,
    "manage": Manage,
}
SECRETS = {("extract", "lichess_token")}


def _owner() -> dict[str, str]:
    """Flat key → the section that owns it."""
    return {f.name: name for name, cls in SECTIONS.items() for f in dataclasses.fields(cls)}


def _build(cls: type, d: dict[str, Any]) -> Any:
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Config:
    extract: Extract = field(default_factory=Extract)
    engine: Engine = field(default_factory=Engine)
    annotate: Annotate = field(default_factory=Annotate)
    select: Select = field(default_factory=Select)
    build: Build = field(default_factory=Build)
    manage: Manage = field(default_factory=Manage)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Config:
        """Sectioned or flat (older versions) settings; a section's own value wins over a flat key."""
        d = dict(d or {})
        if d.get("engine_path") and not d.get("stockfish_path"):  # settings from an early version
            d["stockfish_path"] = d["engine_path"]
        parts: dict[str, dict[str, Any]] = {name: {} for name in SECTIONS}
        owner = _owner()
        for k, v in d.items():
            if k in owner:
                parts[owner[k]][k] = v
        for name in SECTIONS:
            if isinstance(d.get(name), dict):
                parts[name].update(d[name])
        return cls(**{name: _build(SECTIONS[name], parts[name]) for name in SECTIONS})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def flat(self) -> dict[str, Any]:
        """Every setting under its own name, as the add-on stores them."""
        return {k: v for name in SECTIONS for k, v in dataclasses.asdict(getattr(self, name)).items()}

    def fingerprint(self, *sections: str) -> str:
        """Hash of the given sections' settings (all of them when none is named), secrets left out."""
        names = sections or tuple(SECTIONS)
        d = {
            name: {k: v for k, v in dataclasses.asdict(getattr(self, name)).items() if (name, k) not in SECRETS}
            for name in names
        }
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()

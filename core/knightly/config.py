"""Settings, one section per stage. Unknown keys are ignored, so the add-on can keep its own keys alongside.

The stages: extract games → annotate moves → select puzzle candidates →
build puzzles → manage notes. Each stage reads its own section (and `engine`, the shared Stockfish), and each
section has its own fingerprint, so changing one stage's settings only redoes that stage's work.

`Config.from_dict` takes the sectioned form (`{"extract": {...}, "annotate": {...}}`) and also the flat form every
earlier version stored (`{"lichess_user": ..., "sf_depth": ...}`): each flat key goes to the section that owns it,
and the old one-username-per-site keys become accounts of yours.
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


ME, OTHER = "me", "other"
SITES = ("lichess", "chesscom")
SOURCES = (*SITES, "pgn")


@dataclass
class Account:
    """A player whose games are collected. `source` "lichess" or "chesscom": a username there, whose games are
    downloaded. "pgn": a name as PGN headers write it ("Carlsen, Magnus"), matched only in the PGN inputs.
    `role` ME (your own games: puzzles are made from them) or OTHER (players to compare with or to study)."""

    source: str = "lichess"
    name: str = ""
    role: str = ME

    @classmethod
    def from_dict(cls, d: Any) -> Account:
        if isinstance(d, Account):
            return d
        return _build(cls, dict(d))


@dataclass
class Extract:
    """Where games come from and which ones are kept."""

    accounts: list[Account] = field(default_factory=list)
    pgn_paths: list[str] = field(default_factory=list)  # PGN files, or folders searched for *.pgn
    export_dir: str = ""  # after new games arrive, every account's games are written here as PGN; "" = off
    contact_email: str = ""  # sent to Chess.com with each request, as its API asks; "" = none
    lichess_token: str = ""  # a secret: never in a fingerprint or the add-on's config
    history_days: int = 90  # 0 or negative = all time
    time_classes: list[str] = field(default_factory=lambda: ["blitz", "rapid", "classical", "correspondence"])
    rated_only: bool = False
    # When games are fetched (the supervisor): at start, and every so many hours (0 = only at start and by hand).
    # Each later stage runs when the one before it has new output.
    update_on_startup: bool = True
    update_every_hours: float = 6

    def __post_init__(self) -> None:
        seen: set[tuple[str, str]] = set()
        accounts = []
        for a in map(Account.from_dict, self.accounts):
            key = (a.source, a.name.strip().lower())
            if a.source in SOURCES and a.role in (ME, OTHER) and key[1] and key not in seen:
                seen.add(key)
                accounts.append(Account(a.source, a.name.strip(), a.role))
        self.accounts = accounts

    def of(self, source: str, role: str | None = None) -> list[Account]:
        return [a for a in self.accounts if a.source == source and (role is None or a.role == role)]

    def first(self, source: str, role: str = ME) -> str:
        """The first account of this source and role ("" when there is none)."""
        return next((a.name for a in self.of(source, role)), "")

    def has_games(self) -> bool:
        """Something to collect games from."""
        return any(a.source in SITES for a in self.accounts) or bool(self.pgn_paths)


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
    # The supervisor's annotate stage: engine time per run, and whether to keep annotating at low priority
    # afterwards, while the app is open, for at most `keep_annotating_hours` (0 = until everything is annotated).
    annotate_minutes: float = 10
    keep_annotating: bool = False
    keep_annotating_hours: float = 0

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


@dataclass
class Manage:
    """What is done to the notes already in the collection (stages/manage.py)."""

    update_notes: bool = True  # rebuild notes made under older rules; refresh Notes fields as games arrive
    update_tags: bool = True  # a note written again gets the generated tags of now (off: they stay)
    suspend_outdated: bool = False  # suspend the notes this version can no longer maintain


SECTIONS: dict[str, type] = {
    "extract": Extract,
    "engine": Engine,
    "annotate": Annotate,
    "select": Select,
    "build": Build,
    "manage": Manage,
}
SECRETS = {("extract", "lichess_token")}
# When stages run, not what they produce: left out of the fingerprints too.
SCHEDULE = {
    ("extract", "update_on_startup"),
    ("extract", "update_every_hours"),
    ("annotate", "annotate_minutes"),
    ("annotate", "keep_annotating"),
    ("annotate", "keep_annotating_hours"),
}


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
        if "accounts" not in parts["extract"]:  # one username per site, as versions before 0.3 stored them
            parts["extract"]["accounts"] = [
                {"source": site, "name": d[f"{site}_user"], "role": ME} for site in SITES if d.get(f"{site}_user")
            ]
        return cls(**{name: _build(SECTIONS[name], parts[name]) for name in SECTIONS})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def flat(self) -> dict[str, Any]:
        """Every setting under its own name, as the add-on stores them."""
        return {k: v for name in SECTIONS for k, v in dataclasses.asdict(getattr(self, name)).items()}

    def fingerprint(self, *sections: str) -> str:
        """Hash of the given sections' settings (all of them when none is named), secrets and schedules left out."""
        names = sections or tuple(SECTIONS)
        skip = SECRETS | SCHEDULE
        d = {
            name: {k: v for k, v in dataclasses.asdict(getattr(self, name)).items() if (name, k) not in skip}
            for name in names
        }
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def upgrade(stored: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """The add-on's stored settings (over its defaults, `config.json`) brought up to the current version. The
    add-on and the daemon read them the same way."""
    cfg = {**defaults, **stored}
    if cfg.get("config_version", 1) < 2:
        # Version 2: one way of making puzzles (ply by ply). The old grouping settings are gone, and
        # `min_loss` means something else now (a move losing more than this is a puzzle).
        cfg.pop("note_grouping", None)
        cfg.pop("opening_min_loss", None)
        cfg["min_loss"] = defaults.get("min_loss", Select.min_loss)
    if not cfg.get("accounts"):
        # Version 3: accounts (several per site, yours or other players') instead of one username per site.
        # Checked by the old keys, not the version: a config that never stored its version gets the default's.
        cfg["accounts"] = [
            {"source": site, "name": cfg[f"{site}_user"], "role": ME} for site in SITES if cfg.get(f"{site}_user")
        ]
    for old in ("lichess_user", "chesscom_user"):
        cfg.pop(old, None)
    if "keep_running" in cfg:
        # The stages run on their own now: "keep analysing after the update" became annotating in the
        # background (at low priority); cards are only made by the build stage, up to its limit per run.
        if cfg["keep_running"] in ("precalculate", "unlimited"):
            cfg["keep_annotating"] = True
            cfg["keep_annotating_hours"] = float(cfg.get("keep_running_hours") or 0)
        cfg.pop("keep_running", None)
        cfg.pop("keep_running_hours", None)
    if "config_version" in defaults:
        cfg["config_version"] = defaults["config_version"]
    if cfg.get("engine_path") and not cfg.get("stockfish_path"):  # settings from an early version
        cfg["stockfish_path"] = cfg["engine_path"]
    return cfg

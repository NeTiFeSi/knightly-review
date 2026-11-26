"""Data shared by the pipeline stages. Everything is JSON-friendly so it can cross a process boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Game:
    source: str  # "lichess" | "chesscom"
    id: str
    url: str
    white: str
    black: str
    user_is_white: bool
    end_ts: int  # unix seconds
    result: str  # "1-0" | "0-1" | "1/2-1/2" | "*"
    moves: list[str]  # UCI, from the standard start position
    time_class: str = ""
    rated: bool = False
    white_elo: str = ""
    black_elo: str = ""
    event: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    def user_result(self) -> float:
        """1 win, 0.5 draw, 0 loss, from the user's side."""
        if self.result == "1/2-1/2":
            return 0.5
        if self.result in ("1-0", "0-1"):
            return 1.0 if (self.result == "1-0") == self.user_is_white else 0.0
        return 0.5

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Game":
        d = {k: v for k, v in d.items() if k != "site_evals"}  # older caches stored Lichess's evals
        return cls(**d)


class NoWdl(ValueError):
    """A score without the engine's win/draw/loss numbers (the engine must support UCI_ShowWDL)."""


@dataclass(frozen=True)
class Score:
    """Engine score from White's point of view.

    `cp`/`mate` are for display (`[%eval]`). `wdl` is the engine's own win/draw/loss
    estimate in per mille, White's view; all judgements use it.
    """

    cp: Optional[int] = None
    mate: Optional[int] = None
    wdl: Optional[tuple[int, int, int]] = None

    def win_pct(self, white: bool = True) -> float:
        """Expected score 0..100 for the given side: (wins + draws / 2), from the engine's WDL."""
        if self.wdl is None:
            raise NoWdl("score has no WDL")
        w, d, l = self.wdl
        total = (w + d + l) or 1
        own, other = (w, l) if white else (l, w)
        return 100 * (own + d / 2) / total

    def pgn(self) -> str:
        """Value for a `[%eval …]` comment."""
        if self.mate is not None:
            return f"#{self.mate}"
        return f"{(self.cp or 0) / 100:.2f}"

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"cp": self.cp} if self.mate is None else {"mate": self.mate}
        if self.wdl is not None:
            d["wdl"] = list(self.wdl)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Score":
        wdl = d.get("wdl")
        return cls(cp=d.get("cp"), mate=d.get("mate"), wdl=tuple(wdl) if wdl else None)


@dataclass
class Line:
    score: Score
    moves: list[str]  # UCI principal variation


@dataclass
class Eval:
    depth: int
    lines: list[Line]  # best first
    source: str = "engine"
    ms: int = 0  # how long the search took; 0 = not known (results cached before it was stored)

    @property
    def score(self) -> Score:
        return self.lines[0].score

    def to_json(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "source": self.source,
            "lines": [{"score": l.score.to_json(), "moves": l.moves} for l in self.lines],
            **({"ms": self.ms} if self.ms else {}),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Eval":
        return cls(
            depth=d["depth"],
            source=d.get("source", "engine"),
            lines=[Line(Score.from_json(l["score"]), list(l["moves"])) for l in d["lines"]],
            ms=int(d.get("ms", 0)),
        )


@dataclass
class Choice:
    """One candidate move shown on the back of a card."""

    move: str  # UCI
    nag: int
    score: Score  # after the move (White's view)
    reply: list[str] = field(default_factory=list)  # continuation after the move


@dataclass
class CardSpec:
    fen_key: str
    ply: int
    played: Choice
    alternatives: list[Choice]  # best first, without the played move
    loss: float
    comment: str = ""
    mine: bool = True
    motifs: list[str] = field(default_factory=list)  # tactical motifs of the best move, main one first


@dataclass
class NoteSpec:
    """What the add-on writes into the collection. `key` finds the note again."""

    key: str
    pgn: str
    cards: str
    notes: str
    tags: list[str]
    card_ids: dict[str, int]  # fen_key -> cloze number
    new_cards: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

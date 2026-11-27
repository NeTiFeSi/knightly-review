"""Stage 3, select: which annotated positions are puzzle candidates, most promising first.

Input: the annotated positions of a ply (stage 2) and what is on cards already (`Ledger`). Output: the positions
whose worst played move loses more than half of `min_loss` (the scan is shallow: it only rules out the clearly
fine moves), biggest loss first, each with the moves to try at full strength (`tries`). Positions on cards
already, or merged into a similar puzzle, are never offered again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..puzzles import Puzzle, puzzle_id
from .annotate import Unit

CONFIRM_TRIES = 3  # worst moves of a position tried at full strength before giving up on it


@dataclass
class Ledger:
    """What the collection holds, shared by the stages: written by the builder as it makes notes, read by
    the selector so nothing is offered twice."""

    known: dict[str, Puzzle] = field(default_factory=dict)  # puzzle id → puzzle, of the notes made so far
    merged: set[str] = field(default_factory=set)  # puzzle ids merged into a similar puzzle's note
    claimed: set[str] = field(default_factory=set)  # fen keys of every card's position: never made twice

    @classmethod
    def of(cls, known: list[Puzzle], claimed: set[str]) -> Ledger:
        return cls(
            known={p.id: p for p in known},
            merged={bid for p in known for bid in p.links},
            claimed=set(claimed),
        )


class Selector:
    def __init__(self, cfg: Config, ledger: Ledger):
        self.cfg, self.ledger = cfg, ledger

    def floor(self, ply: int) -> float:
        return self.cfg.select.min_loss_at(ply) / 2

    def open(self, units: list[Unit], ply: int) -> list[Unit]:
        """The positions not on a card yet, nor merged into a similar puzzle: the ones worth annotating."""
        led = self.ledger
        out = []
        for u in units:
            if u.pos.key in led.claimed:
                continue
            pid = puzzle_id(u.pos.fen, ply)
            if pid in led.known or pid in led.merged:
                continue
            out.append(u)
        return out

    def rank(self, units: list[Unit], ply: int) -> list[Unit]:
        """The candidates among the scanned positions, biggest loss first."""
        floor = self.floor(ply)
        return sorted((u for u in units if u.loss() > floor), key=lambda u: -u.loss())

    def tries(self, u: Unit, ply: int) -> list[tuple[str, float]]:
        """The moves of a candidate to try at full strength, worst first: at most CONFIRM_TRIES, all above the
        floor."""
        floor = self.floor(ply)
        worst = sorted(u.losses().items(), key=lambda kv: -kv[1])[:CONFIRM_TRIES]
        out = []
        for move, loss in worst:
            if loss <= floor:
                break
            out.append((move, loss))
        return out

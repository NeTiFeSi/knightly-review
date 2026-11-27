"""The run's progress messages, shared by the stages: "<step> (<n> of <want> cards made · <m> min left)"."""

from __future__ import annotations

import math
from collections.abc import Callable

from ..progress import Tracker
from .budget import Budget


class Reporter:
    def __init__(self, tracker: Tracker, budget: Budget, make: bool, done: Callable[[], int]):
        """`done`: how many cards are made (or, when not making cards, puzzles found) so far."""
        self.tracker, self.budget, self.make, self.done = tracker, budget, make, done
        self.step = ""  # the current step's text; a new one forces a progress update

    def msg(self, step: str) -> str:
        left = self.budget.deadline - self.budget.clock()
        budget = "" if left == math.inf else f" · {max(0, math.ceil(left / 60))} min left"
        what = "cards made" if self.make else "puzzles found"
        if not self.budget.capped:
            return f"{step} ({self.done()} {what}{budget})"
        return f"{step} ({self.done()} of {self.budget.want} {what}{budget})"

    def say(self, step: str, detail: str = "", position: str | None = None) -> None:
        """Change the message of the current stage (a forced update when it differs)."""
        self.tracker.budget(self.budget.minutes_left())
        if position is not None:
            self.tracker.position(position)
        self.tracker.message(self.msg(step), step=detail, force=step != self.step)
        self.step = step

    def found(self) -> None:
        """The count changed: report it, and the next message is sent at once."""
        self.tracker.found(self.done(), self.budget.want if self.budget.capped else 0)
        self.step = ""

    def stage(self, step: str, total: int, unit: str, detail: str) -> None:
        """A new stage of `total` `unit`s."""
        self.step = step
        self.tracker.stage("analyse", self.msg(step), total=total, unit=unit, step=detail)

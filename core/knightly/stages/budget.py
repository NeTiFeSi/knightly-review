"""How much one run may do: engine time and cards. Every stage checks it between steps."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

UNBOUNDED = 10**6  # `want` at or above this: no cap, keep confirming (pre-calculating)


class Stop(Exception):
    """The time is up or enough puzzles are made."""


class Budget:
    def __init__(self, minutes: float, want: int, clock: Callable[[], float] = time.monotonic):
        """`minutes` of engine time (0 or less: no limit); `want` new cards (UNBOUNDED: no cap)."""
        self.clock, self.want = clock, want
        self.deadline = clock() + minutes * 60 if minutes > 0 else math.inf

    def out_of_time(self) -> bool:
        return self.clock() >= self.deadline

    def tick(self) -> None:
        if self.out_of_time():
            raise Stop()

    def minutes_left(self) -> float | None:
        """None when there is no time limit."""
        return None if self.deadline == math.inf else max(0.0, (self.deadline - self.clock()) / 60)

    @property
    def capped(self) -> bool:
        return self.want < UNBOUNDED

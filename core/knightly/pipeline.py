"""The one way puzzles are made: ply by ply, the biggest mistakes confirmed first.

`Miner` runs the stages over a set of games, for ply 0, 1, 2… (half-moves already played), the user's positions
first and then, with `cfg.select.include_opponent`, the opponents':

1. select (stages/candidates.py) keeps the positions at that ply not on a card yet;
2. annotate (stages/annotate.py) gives each a cheap scan, which measures the loss of every move played there;
3. select ranks them: those losing more than half of `min_loss`, biggest first;
4. build (stages/build.py) confirms each at full strength and makes its notes;

then the next ply. Every search is cached by position and depth, so a run carries on where the last one stopped.
Before that, the notes built under older rules are rebuilt and the Notes fields refreshed (`Builder.repair`,
`Builder.refresh_all`).

The stages share what is on cards already (`Ledger`), the run's budget (`Budget`) and its messages (`Reporter`).
Here they run one after another, so a run makes exactly the notes the single loop it replaced made; the
supervisor will run them side by side.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from . import stats
from .config import Config
from .model import Game, NoteSpec
from .progress import Tracker
from .puzzles import Puzzle, Store
from .stages.annotate import Annotator, positions
from .stages.budget import UNBOUNDED, Budget, Stop
from .stages.build import Builder
from .stages.candidates import CONFIRM_TRIES, Ledger, Selector
from .stages.report import Reporter

__all__ = ["CONFIRM_TRIES", "UNBOUNDED", "Miner"]

Progress = Callable[[str, int, int, str], None]


class Miner:
    def __init__(
        self,
        games: Iterable[Game],
        analyser,
        cfg: Config,
        store: Store,
        *,
        want: int,
        known: Iterable[Puzzle] = (),
        claimed: Iterable[str] = (),
        existing: dict[str, str] | None = None,
        make: bool = True,
        tracker: Tracker | None = None,
        on_note: Callable[[NoteSpec], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`games` newest first. `known`: the puzzles already in the collection; `claimed`: fen keys of
        the positions of every card already in the collection (never made twice, whatever the ply); `existing`: note key → PGN of the collection's notes.
        `make=False` (pre-calculating): analyse and warm the cache, make and change no notes."""
        self.games = list(games)
        self.cfg, self.make = cfg, make
        self.tracker = tracker or Tracker()
        self.budget = Budget(cfg.build.analysis_minutes, want, clock)
        self.ledger = Ledger.of(list(known), set(claimed))
        self.by_ply = stats.collect(self.games)
        self.annotator = Annotator(analyser, cfg, self.tracker)
        self.selector = Selector(cfg, self.ledger)
        self.report = Reporter(
            self.tracker, self.budget, make, lambda: self.builder.cards if make else self.builder.found
        )
        self.builder = Builder(
            self.games,
            analyser,
            cfg,
            store,
            self.ledger,
            self.annotator,
            self.budget,
            self.report,
            self.tracker,
            existing=existing,
            make=make,
            on_note=on_note,
        )
        self.finished = False  # nothing left to continue: the run ended by itself, not out of time

    # --- what the run did
    @property
    def notes(self) -> list[NoteSpec]:
        return self.builder.notes

    @property
    def analysed(self) -> int:
        """Positions searched by the engine in this run."""
        return self.annotator.analysed

    @property
    def found(self) -> int:
        return self.builder.found

    @property
    def cards(self) -> int:
        return self.builder.cards

    @property
    def unconfirmed(self) -> bool:
        return self.builder.unconfirmed

    def out_of_time(self) -> bool:
        return self.budget.out_of_time()

    # --- the loop
    def run(self) -> None:
        try:
            self.builder.refresh_all()
            self.builder.repair()
            for ply in range(max(self.by_ply, default=-1) + 1):
                for mine in (True, False):
                    if mine or self.cfg.select.include_opponent:
                        self._ply(ply, mine)
            self.finished = True
        except Stop:
            self.finished = self.budget.capped and self.cards >= self.budget.want and self.make

    def _ply(self, ply: int, mine: bool) -> None:
        units = self.selector.open(positions(self.by_ply, ply, mine), ply)
        if not units:
            return
        who = "your moves" if mine else "your opponents' moves"
        depth = self.cfg.annotate.sf_depth
        scan_depth = self.cfg.annotate.scan_depth_used()
        step = f"Ply {ply}, {who}: scanning {len(units)} positions at depth {scan_depth} of {depth}"
        self.report.step = step
        self.tracker.plies(ply, max(self.by_ply, default=0))
        self.tracker.found(self.cards if self.make else self.found, self.budget.want if self.budget.capped else 0)
        self.tracker.budget(self.budget.minutes_left())
        self.report.stage(step, len(units), "positions", f"depth {scan_depth}")
        self.annotator.scan(units, ply, self.budget.tick)
        for u in self.selector.rank(units, ply):
            self.budget.tick()
            self.builder.confirm(u, ply, self.selector.tries(u, ply))
            if self.make and self.cards >= self.budget.want:
                raise Stop()

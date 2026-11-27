"""The stages that make puzzles, one at a time: annotate (losses), select (candidates), the budget and messages."""

from __future__ import annotations

import unittest

import chess
from helpers import SICILIAN, game_from, score_for
from knightly import stats
from knightly.config import Config
from knightly.model import Line
from knightly.progress import Tracker
from knightly.puzzles import Puzzle, puzzle_id
from knightly.stages.annotate import positions
from knightly.stages.budget import UNBOUNDED, Budget, Stop
from knightly.stages.candidates import CONFIRM_TRIES, Ledger, Selector
from knightly.stages.report import Reporter


def scanned(unit, best_pct: float, played: dict[str, float]):
    """Give `unit` a scan: the best line at `best_pct` and each played move at its win rate (side to move)."""
    white = unit.board.turn == chess.WHITE
    pct = lambda p: score_for(p if white else 100 - p)
    unit.scan = (Line(pct(best_pct), ["best"]), {m: Line(pct(p), [m]) for m, p in played.items()})
    return unit


class AnnotateTest(unittest.TestCase):
    def test_positions_of_one_side(self):
        g = game_from(SICILIAN, white=False, id="g1")
        by_ply = stats.collect([g])
        mine = positions(by_ply, 1, True)
        self.assertEqual([list(u.played) for u in mine], [["c7c5"]])
        self.assertEqual(positions(by_ply, 1, False), [])  # your opponent didn't move at ply 1
        self.assertEqual([list(u.played) for u in positions(by_ply, 0, False)], [["e2e4"]])

    def test_losses_from_the_scan(self):
        u = positions(stats.collect([game_from(SICILIAN, white=False, id="g1")]), 1, True)[0]
        self.assertEqual((u.losses(), u.loss()), ({}, 0.0))  # not scanned yet
        scanned(u, 60, {"c7c5": 45})
        self.assertAlmostEqual(u.loss(), 15, places=0)


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.g = game_from(SICILIAN, white=False, id="g1")
        self.by_ply = stats.collect([self.g])
        self.cfg = Config.from_dict({"min_loss": 4})

    def unit(self, ply=1):
        return positions(self.by_ply, ply, ply % 2 == 1)[0]

    def test_positions_on_cards_or_merged_are_not_offered(self):
        u = self.unit()
        self.assertEqual(Selector(self.cfg, Ledger()).open([u], 1), [u])
        self.assertEqual(Selector(self.cfg, Ledger(claimed={u.pos.key})).open([u], 1), [])
        self.assertEqual(Selector(self.cfg, Ledger(merged={puzzle_id(u.pos.fen, 1)})).open([u], 1), [])
        known = Puzzle(key="k", fen=u.pos.fen, ply=1)
        self.assertEqual(Selector(self.cfg, Ledger.of([known], set())).open([u], 1), [])
        self.assertEqual(Selector(self.cfg, Ledger.of([known], set())).open([u], 3), [u])  # another ply

    def test_biggest_loss_first_above_half_of_min_loss(self):
        a, b, c = (
            scanned(self.unit(p), 60, {m: 60 - loss})
            for p, m, loss in ((1, "c7c5", 3), (3, "d7d6", 9), (5, "c5d4", 1.5))
        )
        self.assertEqual(Selector(self.cfg, Ledger()).rank([a, b, c], 1), [b, a])  # 1.5 is not above 2

    def test_tries_the_worst_moves_first(self):
        u = scanned(self.unit(), 60, {"a": 59, "b": 30, "c": 50, "d": 40, "e": 45})
        tries = Selector(self.cfg, Ledger()).tries(u, 1)
        self.assertEqual([m for m, _ in tries], ["b", "d", "e"][:CONFIRM_TRIES])
        u = scanned(self.unit(), 60, {"a": 59, "b": 30})
        self.assertEqual([m for m, _ in Selector(self.cfg, Ledger()).tries(u, 1)], ["b"])  # "a" loses 1: below


class BudgetTest(unittest.TestCase):
    def test_time_and_cards(self):
        t = [0.0]
        b = Budget(1, 5, clock=lambda: t[0])
        b.tick()
        self.assertEqual((b.minutes_left(), b.capped), (1.0, True))
        t[0] = 60
        with self.assertRaises(Stop):
            b.tick()
        free = Budget(0, UNBOUNDED)
        free.tick()
        self.assertEqual((free.minutes_left(), free.capped), (None, False))

    def test_messages(self):
        t = [0.0]
        cards = [2]
        r = Reporter(Tracker(), Budget(3, 10, clock=lambda: t[0]), True, lambda: cards[0])
        self.assertEqual(r.msg("Ply 4"), "Ply 4 (2 of 10 cards made · 3 min left)")
        pre = Reporter(Tracker(), Budget(0, UNBOUNDED), False, lambda: 7)
        self.assertEqual(pre.msg("Ply 4"), "Ply 4 (7 puzzles found)")


if __name__ == "__main__":
    unittest.main()

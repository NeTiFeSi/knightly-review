"""The statistics page: the numbers from the stages' tables, and the page drawn from them."""

from __future__ import annotations

import json
import re
import unittest

from helpers import FakeAnalyser
from knightly import dashboard
from knightly.config import Config
from knightly.supervisor import Supervisor
from test_core import CFG, PUNISH_TABLE, recent, seed
from test_supervisor import context


class DashboardTest(unittest.TestCase):
    def test_counts_your_annotated_moves(self):
        c = seed(self, [recent()])  # the scholar's mate, you playing Black: 3...Nf6?? and mated
        cfg = Config.from_dict(CFG)
        before = dashboard.collect(cfg, c)
        self.assertEqual((before.games, before.total.moves, before.notes), (1, 0, None))
        Supervisor(context(c, cfg, lambda *a: FakeAnalyser(table=PUNISH_TABLE))).run_stage("annotate")
        c.publish_notes({"puzzle:5:abc": "1. e4 *", "legacy:3": "1. d4 *"})
        st = dashboard.collect(cfg, c)
        self.assertEqual((st.games, st.your_moves, st.total.moves), (1, 3, 3))
        self.assertEqual(st.total.blunders, 1)
        self.assertEqual(st.colors["black"], [0, 0, 1])
        self.assertEqual(st.phases["opening"].blunders, 1)
        self.assertEqual(st.time_classes["blitz"].games, 1)
        self.assertEqual(st.notes, 1)  # puzzle notes only
        page = dashboard.render(st, "dark")
        self.assertIn('data-theme="dark"', page)
        self.assertIn("By phase of the game", page)
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page).group(1))
        self.assertEqual(data["total"]["blunders"], 1)
        self.assertIn('<html lang="en">', dashboard.render(st))  # a saved page follows its reader
        self.assertIn("1 game · 3 moves annotated", page)

    def test_no_games(self):
        page = dashboard.render(dashboard.collect(Config.from_dict(CFG), seed(self, [])))
        self.assertIn("No annotated moves yet.", page)

    def test_axis_tops(self):
        self.assertEqual([dashboard._nice(v) for v in (0.5, 3, 17.4, 22, 60, 130)], [1, 5, 20, 25, 100, 200])


if __name__ == "__main__":
    unittest.main()

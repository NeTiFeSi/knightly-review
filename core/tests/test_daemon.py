"""The daemon: turns with the add-on, the notes the add-on publishes, and reading the add-on's folder."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import FakeAnalyser
from knightly.config import Config
from knightly.daemon import STAGES, AddonFolder, Daemon
from knightly.engine.broker import Pool
from knightly.supervisor import NOTE, WANT_KEY, Context, Outcome, Supervisor, Turn
from test_core import CFG, PUNISH_TABLE, recent, seed, tmpcache

ADDON = Path(__file__).resolve().parents[2] / "addon" / "knightly_review"


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


class TurnTest(unittest.TestCase):
    def test_the_add_on_comes_first(self):
        c = tmpcache(self)
        stop = threading.Event()
        daemon, addon = Turn(c, first=False, owner="daemon"), Turn(c, first=True, owner="addon")
        self.assertTrue(daemon.take(stop))
        self.assertFalse(daemon.yield_now())
        # Anki opens: the add-on asks, and waits while the daemon holds the turn.
        stop.set()
        self.assertFalse(addon.take(stop))
        stop.clear()
        self.assertTrue(daemon.yield_now())
        daemon.release()
        self.assertTrue(addon.take(stop))
        # The daemon waits while the add-on wants the turn, even between its runs.
        addon.cache.unclaim("stages_turn", "addon")
        stop.set()
        self.assertFalse(daemon.take(stop))
        stop.clear()
        # Anki closes: the daemon goes on.
        addon.release()
        self.assertIsNone(c.get(WANT_KEY))
        self.assertTrue(daemon.take(stop))

    def test_a_lapsed_wish_doesnt_block_the_daemon(self):
        c = tmpcache(self)
        c.claim(WANT_KEY, "crashed anki", 0.01)
        time.sleep(0.02)
        self.assertTrue(Turn(c, first=False, owner="daemon").take(threading.Event()))


class HandOverTest(unittest.TestCase):
    def test_the_daemon_hands_over_and_runs_again_afterwards(self):
        c = tmpcache(self)
        runs: list[str] = []
        started = threading.Event()

        def annotate(ctx, tracker, manual):
            runs.append("start")
            started.set()
            if ctx.cancel.wait(5):  # a long run, until cancelled
                runs.append("cancelled")
                from knightly.http import Cancelled

                raise Cancelled()
            return Outcome()

        cfg = Config.from_dict({**CFG, "update_on_startup": False})
        ctx = Context(cfg, c, Pool(cfg), threading.Event(), existing=dict)
        workers = {n: (lambda *a: Outcome()) for n in STAGES} | {"annotate": annotate}
        sup = Supervisor(ctx, workers=workers, stages=STAGES, turn=Turn(c, first=False, owner="daemon"))
        sup.tick = 0.05
        sup.start()
        sup.trigger("annotate")
        self.assertTrue(started.wait(5))
        addon = Turn(c, first=True, owner="addon")
        c.claim(WANT_KEY, "addon", 60)  # Anki opens
        wait_until(lambda: runs == ["start", "cancelled"])
        self.assertTrue(addon.take(threading.Event()))
        time.sleep(0.2)
        self.assertEqual(runs, ["start", "cancelled"])  # waits while Anki runs
        started.clear()
        addon.release()  # Anki closes: the cancelled run starts again
        self.assertTrue(started.wait(5))
        sup.stop(wait=5)
        self.assertNotIn("manage", sup.state)


class DaemonTest(unittest.TestCase):
    def daemon(self, c, **kw):
        cfg = Config.from_dict({**CFG, "analysis_minutes": 0, "max_new_cards": 1000, "update_on_startup": False})
        d = Daemon(lambda: cfg, c, log=lambda _l: None, analyser_factory=lambda *a: FakeAnalyser(table=PUNISH_TABLE))
        self.addCleanup(d.pool.close)
        return d

    def test_puzzles_wait_for_the_add_on_to_publish_its_notes(self):
        c = seed(self, [recent()])
        d = self.daemon(c)
        self.assertTrue(d.sup.ctx.cfg.annotate.keep_annotating)
        d.sup.run_stage("annotate")
        self.assertEqual(d.sup.run_stage("select").summary, "no collection")
        c.publish_notes({})
        d.sup.run_stage("select")
        (note,) = d.sup.run_stage("build").notes
        self.assertEqual([k for _s, k, _d in c.outbox()], [NOTE])
        # The add-on drains the outbox, publishing the note with it: the daemon doesn't make it again.
        c.outbox_done([s for s, *_ in c.outbox()], notes={note.key: note.pgn})
        self.assertEqual(c.published_notes(), {note.key: note.pgn})
        d.sup.run_stage("select")
        self.assertEqual(d.sup.run_stage("build").summary, "no candidates")

    def test_new_settings_are_read(self):
        c = seed(self, [])
        stamp = [1]
        cfgs = [Config.from_dict({**CFG, "max_new_cards": 5}), Config.from_dict({**CFG, "max_new_cards": 7})]
        d = Daemon(lambda: cfgs[0], c, log=lambda _l: None, stamp=lambda: stamp[0])
        self.addCleanup(d.pool.close)
        cfgs.pop(0)
        d.tick()
        self.assertEqual(d.sup.ctx.cfg.build.max_new_cards, 5)
        stamp[0] = 2
        d.tick()
        self.assertEqual(d.sup.ctx.cfg.build.max_new_cards, 7)
        self.assertTrue(d.sup.ctx.cfg.annotate.keep_annotating)


class AddonFolderTest(unittest.TestCase):
    def test_reads_the_add_on_settings(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        with self.assertRaises(ValueError):
            AddonFolder(d)
        shutil.copy(ADDON / "config.json", d / "config.json")
        folder = AddonFolder(d)
        before = folder.stamp()
        (d / "meta.json").write_text(json.dumps({"config": {"lichess_user": "old", "max_new_cards": 4}}))
        (d / "user_files").mkdir()
        (d / "user_files" / "secrets.json").write_text(json.dumps({"lichess_token": "t"}))
        self.assertNotEqual(folder.stamp(), before)
        cfg = folder.config()
        self.assertEqual([(a.source, a.name) for a in cfg.extract.accounts], [("lichess", "old")])
        self.assertEqual((cfg.build.max_new_cards, cfg.extract.lichess_token), (4, "t"))
        self.assertEqual(folder.cache_path, d / "user_files" / "cache.sqlite")


if __name__ == "__main__":
    unittest.main()

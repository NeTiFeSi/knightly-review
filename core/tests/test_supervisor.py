"""The stages run by the supervisor, through their tables: the same notes as one update run, and the chaining."""

from __future__ import annotations

import threading
import time
import unittest

from helpers import SICILIAN, FakeAnalyser, S, ScriptedAnalyser, board_at, game_from
from knightly.cache import fen_key
from knightly.config import Config
from knightly.engine.broker import Pool
from knightly.jobs import run_update
from knightly.model import NoteSpec
from knightly.supervisor import NEXT, NOTE, STAGES, SUSPEND, Context, Outcome, Supervisor
from test_core import B_MOVES, B_TABLE, CFG, PUNISH_TABLE, SubNoteTest, recent, seed


def note_tuple(n: NoteSpec) -> tuple:
    return (n.key, n.pgn, n.cards, n.notes, tuple(n.tags), n.new_cards)


def context(cache, cfg: Config, factory, existing=None) -> Context:
    return Context(
        cfg=cfg,
        cache=cache,
        pool=Pool(cfg),
        cancel=threading.Event(),
        existing=lambda: existing if existing is not None else {},
        analyser_factory=factory,
    )


def outbox_notes(cache) -> list[NoteSpec]:
    return [NoteSpec(**d) for _seq, kind, d in cache.outbox() if kind == NOTE]


class SameNotesTest(unittest.TestCase):
    """With no limits, running every stage once makes the notes one update run makes."""

    def compare(self, games, factory, **cfg):
        config = Config.from_dict({**CFG, "analysis_minutes": 0, "max_new_cards": 1000, **cfg})
        want = run_update(config, seed(self, games), fetch=False, existing={}, analyser_factory=factory).notes
        c = seed(self, games)
        sup = Supervisor(context(c, config, factory))
        for name in ("manage", "annotate", "select", "build"):  # no extract: the games are there already
            sup.run_stage(name)
        got = outbox_notes(c)
        self.assertTrue(want)
        self.assertEqual(sorted(map(note_tuple, got)), sorted(map(note_tuple, want)))
        return c, got

    def test_one_mistake(self):
        self.compare([recent()], lambda *a: FakeAnalyser(table=PUNISH_TABLE))

    def test_a_tree_with_notes_inside(self):
        table = SubNoteTest("pos").table()
        self.compare([recent(url="https://lichess.org/test0001")], lambda *a: FakeAnalyser(table=table))

    def test_a_similar_position_is_merged(self):
        a = recent()
        b = recent(id="test0002", moves=list(B_MOVES), end_ts=int(time.time()) - 60)

        def an(*_):
            f = FakeAnalyser(table={**PUNISH_TABLE, **B_TABLE})
            f.scores[fen_key(board_at(B_MOVES, 6).fen())] = S(400, 800, 200, 0)
            return f

        self.compare([a, b], an)

    def test_several_games_and_your_opponents(self):
        now = int(time.time())
        g1 = game_from(SICILIAN, white=False, id="g1", end_ts=now - 300)
        g2 = game_from(["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6"], white=False, id="g2", end_ts=now - 200)
        script = {"g1": {5: 30, 4: 20}, "g2": {5: 15}}
        self.compare([g1, g2], lambda *a: ScriptedAnalyser([g1, g2], script), include_opponent=True)


class TablesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict({**CFG, "analysis_minutes": 0, "max_new_cards": 1000})
        self.c = seed(self, [recent()])
        self.sup = Supervisor(context(self.c, self.cfg, lambda *a: FakeAnalyser(table=PUNISH_TABLE)))

    def test_each_stage_reads_the_last_ones_table(self):
        self.assertEqual(self.sup.run_stage("select").progressed, False)  # nothing annotated yet
        ann = self.sup.run_stage("annotate")
        self.assertTrue(ann.progressed)
        self.assertEqual(self.sup.run_stage("annotate").summary, "every position is annotated")
        self.assertTrue(self.sup.run_stage("select").progressed)
        self.assertEqual(len(self.c.candidates()), 1)
        built = self.sup.run_stage("build")
        self.assertEqual((len(built.notes), self.c.candidates()), (1, []))
        self.assertEqual(self.sup.run_stage("build").summary, "no candidates")

    def test_a_candidate_on_a_card_already_is_skipped(self):
        self.sup.run_stage("annotate")
        self.sup.run_stage("select")
        self.sup.run_stage("build")
        (note,) = outbox_notes(self.c)
        # The note is in the collection now (the outbox drained), and new settings judge everything again.
        self.c.outbox_done([seq for seq, *_ in self.c.outbox()])
        self.sup.ctx.existing = lambda: {note.key: note.pgn}
        self.sup.ctx.cfg = Config.from_dict({**CFG, "analysis_minutes": 0, "max_new_cards": 1000, "min_loss": 0.5})
        self.sup.run_stage("select")
        self.assertEqual(self.c.candidates(), [])  # its position is on a card: never a candidate again
        self.assertEqual(self.sup.run_stage("build").notes, [])

    def test_notes_in_the_outbox_count_as_on_cards(self):
        self.sup.run_stage("annotate")
        self.sup.run_stage("select")
        self.sup.run_stage("build")
        self.c.add_candidates([(row[0], row[1], row[2], 1.0, [["x", 1.0]]) for row in self.c.candidates("made")])
        self.assertEqual(self.sup.run_stage("build").notes, [])  # not made twice while waiting in the outbox

    def test_the_card_limit_leaves_candidates_waiting(self):
        g1 = game_from(SICILIAN, white=False, id="g1", end_ts=int(time.time()) - 300)
        g2 = game_from(["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6"], white=False, id="g2", end_ts=int(time.time()))
        c = seed(self, [g1, g2])
        cfg = Config.from_dict({**CFG, "analysis_minutes": 0, "max_new_cards": 1})  # one card per note here
        sup = Supervisor(context(c, cfg, lambda *a: ScriptedAnalyser([g1, g2], {"g1": {5: 30}, "g2": {5: 15}})))
        for name in ("annotate", "select"):
            sup.run_stage(name)
        first = sup.run_stage("build")
        self.assertEqual((sum(n.new_cards for n in first.notes), first.more), (1, True))
        second = sup.run_stage("build")  # the next run carries on with the next candidate
        self.assertEqual((sum(n.new_cards for n in second.notes), second.more), (1, False))
        self.assertNotEqual(first.notes[0].key, second.notes[0].key)

    def test_out_of_date_notes_go_to_the_outbox(self):
        cfg = Config.from_dict({**CFG, "suspend_outdated": True})
        sup = Supervisor(context(self.c, cfg, lambda *a: FakeAnalyser(table=PUNISH_TABLE), {"legacy:7": "1. e4 *"}))
        sup.run_stage("manage")
        self.assertIn((SUSPEND, ["legacy:7"]), [(k, d) for _s, k, d in self.c.outbox()])
        self.assertEqual(sup.run_stage("manage").summary, "nothing changed")


class ChainTest(unittest.TestCase):
    def test_stages_run_on_their_threads_in_a_chain(self):
        ran: list[str] = []
        done = threading.Event()

        def worker(name, progressed=True):
            def run(ctx, tracker, manual):
                ran.append(name)
                if name == "build":
                    done.set()
                return Outcome(progressed)

            return run

        cfg = Config.from_dict({**CFG, "update_on_startup": True})
        sup = Supervisor(context(seed(self, []), cfg, None), workers={n: worker(n) for n in STAGES})
        sup.start()
        self.assertTrue(done.wait(5))
        sup.stop(wait=5)
        self.assertEqual(ran[0], "extract")
        self.assertLess(ran.index("annotate"), ran.index("select"))
        self.assertLess(ran.index("select"), ran.index("build"))
        self.assertIn("manage", ran)
        self.assertEqual(NEXT["extract"], ("manage", "annotate"))

    def test_nothing_new_stops_the_chain(self):
        ran: list[str] = []
        idle = threading.Event()

        def run(name):
            def f(ctx, tracker, manual):
                ran.append(name)
                if name == "annotate":
                    idle.set()
                return Outcome(False)

            return f

        cfg = Config.from_dict({**CFG, "update_on_startup": False})
        sup = Supervisor(context(seed(self, []), cfg, None), workers={n: run(n) for n in STAGES})
        sup.start()
        time.sleep(0.2)
        self.assertEqual(ran, [])  # nothing at start when not asked
        sup.update()
        self.assertTrue(idle.wait(5))
        time.sleep(0.2)
        sup.stop(wait=5)
        self.assertNotIn("select", ran)  # annotate wrote nothing: select isn't woken

    def test_a_failing_stage_is_reported_and_the_others_go_on(self):
        seen = []

        def boom(ctx, tracker, manual):
            raise RuntimeError("no engine")

        sup = Supervisor(
            context(seed(self, []), Config.from_dict(CFG), None),
            workers={**{n: (lambda *a: Outcome()) for n in STAGES}, "annotate": boom},
            on_done=lambda name, out, err: seen.append((name, err)),
        )
        sup.run_all()
        self.assertIn(("annotate", "no engine"), seen)
        self.assertIn(("build", ""), seen)

    def test_keep_annotating_runs_again_while_work_is_left(self):
        passes = []
        enough = threading.Event()

        def annotate(ctx, tracker, manual):
            passes.append(ctx.background)
            if len(passes) == 3:
                enough.set()
            return Outcome(True, more=len(passes) < 3)

        cfg = Config.from_dict({**CFG, "update_on_startup": False, "keep_annotating": True})
        workers = {**{n: (lambda *a: Outcome()) for n in STAGES}, "annotate": annotate}
        sup = Supervisor(context(seed(self, []), cfg, None), workers=workers)
        sup.start()
        sup.trigger("annotate")
        self.assertTrue(enough.wait(5))
        time.sleep(0.1)
        sup.stop(wait=5)
        self.assertEqual(passes, [False, True, True])  # the first pass at its own priority, then low priority


if __name__ == "__main__":
    unittest.main()

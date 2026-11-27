"""Note type, media and note writes against a throwaway collection (needs the `anki` package and a dev build)."""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

ADDON = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADDON))

try:
    from anki.collection import Collection
except ImportError:  # pragma: no cover
    Collection = None

HAVE_BUILD = (ADDON / "knightly_review" / "vendor" / "knightly").is_dir() and (
    ADDON / "knightly_review" / "card"
).is_dir()


@unittest.skipUnless(Collection and HAVE_BUILD, "needs `anki` and `python3 build.py --dev`")
class CollectionTest(unittest.TestCase):
    def setUp(self):
        from knightly_review import collection as kc

        self.kc = kc
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.col = Collection(str(self.dir / "c.anki2"))
        self.addCleanup(self.col.close)
        self.card_dir = self.dir / "card"
        shutil.copytree(ADDON / "knightly_review" / "card", self.card_dir)

    KEY = "puzzle:5:abcdef123456"

    def spec(self, key=KEY, cards="{{c1::1::1}}", pgn="1. e4 { [%kr 1] } e5 *", tags=("knightly::phase::opening",)):
        from knightly_review.vendor.knightly import notes as kn
        from knightly_review.vendor.knightly.model import NoteSpec

        return NoteSpec(
            key=key, pgn=pgn, cards=cards, notes="n", tags=["knightly", kn.note_key_tag(key), *tags], card_ids={}
        )

    def test_install_is_idempotent_and_keeps_config(self):
        self.assertEqual(
            self.kc.ensure_installed(self.col, self.card_dir), ["_knightly.js updated", "note type created"]
        )
        self.assertEqual(self.kc.ensure_installed(self.col, self.card_dir), [])
        nt = self.col.models.by_name(self.kc.MODEL)
        self.assertEqual([f["name"] for f in nt["flds"]], ["PGN", "Cards", "Notes", "Visits"])
        # The user saved their own settings; a new build must keep them.
        t = nt["tmpls"][0]
        t["qfmt"] = self.kc.BLOCK_RE.sub("window.KR_CONFIG = { hints: false };", t["qfmt"])
        t["afmt"] = self.kc.BLOCK_RE.sub("window.KR_CONFIG = {\n  acceptNags: [1, 3, 5],\n  hints: true\n};", t["afmt"])
        self.col.models.update_dict(nt)
        (self.card_dir / "front.html").write_text((self.card_dir / "front.html").read_text() + "\n<!-- v2 -->")
        (self.card_dir / "_knightly.js").write_bytes(b"// v2")
        changes = self.kc.ensure_installed(self.col, self.card_dir)
        self.assertEqual(changes, ["_knightly.js updated", "note type updated (your card settings kept)"])
        qfmt = self.col.models.by_name(self.kc.MODEL)["tmpls"][0]["qfmt"]
        self.assertIn("hints: false", qfmt)
        afmt = self.col.models.by_name(self.kc.MODEL)["tmpls"][0]["afmt"]
        self.assertIn("acceptNags: [1, 3, 5, 8],", afmt)  # old default upgraded
        self.assertIn("<!-- v2 -->", qfmt)
        media = pathlib.Path(self.col.media.dir()) / "_knightly.js"
        self.assertEqual(media.read_bytes(), b"// v2")

    def test_run_writer_adds_as_it_goes_and_merges_into_one_undo_step(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        w = self.kc.RunWriter(self.col, "Chess::Knightly")
        w.add(self.spec())
        self.assertEqual(self.kc.count_notes(self.col), 1)  # already there before the run ends
        two = self.spec(cards="{{c1::1::1}}\n{{c2::2::1}}", pgn="1. e4 { [%kr 1] } e5 { [%kr 2] } *")
        w.add(two)  # the same note, updated in place
        w.add(self.spec(key="puzzle:6:fedcba654321"))
        self.assertEqual(self.kc.count_notes(self.col), 2)
        w.finish()
        self.assertTrue(w.clean)
        self.assertEqual(self.col.undo_status().undo, self.kc.UNDO_NAME)
        self.col.undo()
        self.assertEqual(self.kc.count_notes(self.col), 0)

    def test_run_writer_does_not_swallow_other_undo_steps(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        w = self.kc.RunWriter(self.col, "K")
        w.add(self.spec())
        other = self.col.new_note(self.col.models.by_name(self.kc.MODEL))
        other["PGN"] = "x"
        self.col.add_note(other, self.col.decks.id("K"))  # the user's own change in between
        w.add(self.spec(key="puzzle:6:fedcba654321"))
        w.finish()
        self.assertFalse(w.clean)
        self.col.undo()  # undoes only the last write
        self.assertEqual(len(self.col.find_notes(f'"note:{self.kc.MODEL}"')), 2)

    def test_apply_adds_then_updates(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec()], "Chess::Knightly")
        self.assertEqual(self.col.card_count(), 1)
        self.assertEqual(self.kc.existing_notes(self.col), {self.KEY: "1. e4 { [%kr 1] } e5 *"})
        two = self.spec(cards="{{c1::1::1}} {{c2::2::2}}", pgn="1. e4 { [%kr 1] } e5 { [%kr 2] } *")
        self.kc.apply_notes(self.col, [two], "Chess::Knightly")
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(self.col.card_count(), 2)
        self.assertIn("Chess::Knightly", [d.name for d in self.col.decks.all_names_and_ids()])
        self.assertEqual(self.kc.count_notes(self.col), 1)

    def test_sibling_bury_hint(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.assertIsNone(self.kc.sibling_bury_hint(self.col, "KR", 20, 7))  # deck doesn't exist yet
        did = self.col.decks.id("KR")
        conf = self.col.decks.config_dict_for_deck_id(did)
        conf["new"]["bury"] = False
        self.col.decks.update_config(conf)
        self.assertIsNone(self.kc.sibling_bury_hint(self.col, "KR", 20, 7))
        conf["new"]["bury"] = True
        self.col.decks.update_config(conf)
        self.assertIn("20 new cards are in 7 notes", self.kc.sibling_bury_hint(self.col, "KR", 20, 7))
        self.assertIsNone(self.kc.sibling_bury_hint(self.col, "KR", 7, 7))  # one card per note: nothing hidden

    def test_game_note_tags_are_replaced(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec(tags=("knightly::result::loss", "knightly::severity::mistake"))], "K")
        note = self.col.get_note(self.col.find_notes("tag:knightly")[0])
        note.add_tag("my-own")
        self.col.update_note(note)
        self.kc.apply_notes(self.col, [self.spec(tags=("knightly::result::loss", "knightly::severity::blunder"))], "K")
        tags = set(self.col.get_note(note.id).tags)
        self.assertIn("knightly::severity::blunder", tags)
        self.assertNotIn("knightly::severity::mistake", tags)
        self.assertIn("my-own", tags)
        self.assertIn(self.kc.kn.note_key_tag(self.KEY), tags)

    def test_visits_field_is_added_to_an_older_note_type_and_merged_per_card(self):
        import json
        from types import SimpleNamespace

        from knightly_review import _flush_visits, _queue_visits

        self.kc.ensure_installed(self.col, self.card_dir)
        mm = self.col.models
        nt = mm.by_name(self.kc.MODEL)
        mm.remove_field(nt, next(f for f in nt["flds"] if f["name"] == "Visits"))  # an older note type
        mm.update_dict(nt)
        self.assertIn("Visits field added", self.kc.ensure_installed(self.col, self.card_dir))
        self.kc.apply_notes(self.col, [self.spec()], "KR")
        note = self.col.get_note(self.col.find_notes('"note:Knightly Review"')[0])
        card = SimpleNamespace(id=1, note=lambda: self.col.get_note(note.id))
        self.assertEqual(_queue_visits(card, 'k1:1|{"r1":2,"r2":1}'), {"ok": True})
        self.assertEqual(_queue_visits(card, 'k1:1|{"r1":1,"r3":4}'), {"ok": True})  # the larger count wins
        self.assertEqual(_queue_visits(card, 'k2:2|{"r1":9}'), {"ok": True})  # another card of the note
        self.assertEqual(self.col.get_note(note.id)["Visits"], "")  # nothing written before the answer
        self.assertFalse(_flush_visits(self.col, card))  # the last step is not an answer: they stay queued
        self.assertTrue(_flush_visits(self.col, card, answered=False))  # the profile's close
        stored = json.loads(self.col.get_note(note.id)["Visits"])
        self.assertEqual(stored, {"k1:1": {"r1": 2, "r2": 1, "r3": 4}, "k2:2": {"r1": 9}})
        _queue_visits(card, 'k2:2|{"r1":3}')
        self.assertFalse(_flush_visits(self.col, card, answered=False))  # lower counts change nothing
        self.assertFalse(_flush_visits(self.col, card, answered=False))  # nothing queued

    def test_visits_join_the_answer_undo_step(self):
        import json
        from types import SimpleNamespace

        from anki.scheduler.v3 import CardAnswer
        from knightly_review import _flush_visits, _queue_visits

        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec()], "KR")
        before = self.col.undo_status().undo
        c = self.col.get_card(self.col.find_cards('"note:Knightly Review"')[0])
        c.start_timer()
        states = self.col._backend.get_scheduling_states(c.id)
        self.col.sched.answer_card(self.col.sched.build_answer(card=c, states=states, rating=CardAnswer.GOOD))
        answered = self.col.undo_status().undo
        note = lambda: self.col.get_note(c.nid)
        card = SimpleNamespace(id=c.id, note=note)
        _queue_visits(card, 'k1:1|{"r1":1}')
        self.assertTrue(_flush_visits(self.col, card))
        self.assertEqual(json.loads(note()["Visits"]), {"k1:1": {"r1": 1}})
        self.assertEqual(self.col.undo_status().undo, answered)  # still "Answer Card", no step of its own
        self.col.undo()  # undoes the answer and the counts together
        self.assertEqual(self.col.get_card(c.id).reps, 0)
        self.assertEqual(note()["Visits"], "")
        self.assertEqual(self.col.undo_status().undo, before)

    def test_visits_left_queued_are_written_when_the_profile_closes(self):
        import json

        from anki.scheduler.v3 import CardAnswer
        from knightly_review import _flush_all_visits, _flush_visits, _pending_visits, _queue_visits

        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec()], "KR")
        c = self.col.get_card(self.col.find_cards('"note:Knightly Review"')[0])
        c.start_timer()
        states = self.col._backend.get_scheduling_states(c.id)
        self.col.sched.answer_card(self.col.sched.build_answer(card=c, states=states, rating=CardAnswer.GOOD))
        self.kc.apply_notes(self.col, [self.spec(key="puzzle:6:fedcba654321")], "KR")  # another change on top
        top = self.col.undo_status().undo
        _queue_visits(c, 'k1:1|{"r1":1}')
        _queue_visits(SimpleNamespace(id=999999), 'k9:1|{"r1":1}')  # a card deleted since
        self.assertFalse(_flush_visits(self.col, c))  # not merged into someone else's step: still queued
        self.assertEqual(self.col.undo_status().undo, top)
        _flush_all_visits(self.col)
        self.assertEqual(json.loads(self.col.get_note(c.nid)["Visits"]), {"k1:1": {"r1": 1}})
        self.assertEqual(_pending_visits, {})

    def test_a_note_can_keep_its_tags(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec(tags=("knightly::severity::mistake",))], "K")
        again = self.spec(tags=("knightly::severity::blunder",), pgn="1. e4 { [%kr 1] } c5 *")
        again.retag = False
        self.kc.apply_notes(self.col, [again], "K")
        note = self.col.get_note(self.col.find_notes("tag:knightly")[0])
        self.assertIn("knightly::severity::mistake", note.tags)
        self.assertNotIn("knightly::severity::blunder", note.tags)
        self.assertEqual(self.kc.kn.field_to_pgn(note["PGN"]), "1. e4 { [%kr 1] } c5 *")  # the rest is written
        fresh = self.spec(key="puzzle:6:fedcba654321", tags=("knightly::severity::blunder",))
        fresh.retag = False
        self.kc.apply_notes(self.col, [fresh], "K")  # a new note gets its tags anyway
        self.assertIn("knightly::severity::blunder", self.col.get_note(self.kc.note_ids(self.col, [fresh.key])[0]).tags)

    def test_outdated_notes_are_suspended_in_the_runs_undo_step(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec()], "K")
        old = self.col.new_note(self.col.models.by_name(self.kc.MODEL))
        old["PGN"], old["Cards"] = "1. d4 { [%kr 1] } d5 *", "{{c1::1::1}}"
        old.add_tag("knightly")
        self.col.add_note(old, self.col.decks.id("K"))
        w = self.kc.RunWriter(self.col, "K")
        w.add(self.spec(key="puzzle:6:fedcba654321"))
        w.suspend([f"legacy:{old.id}", self.KEY, "puzzle:9:not-in-the-collection"])
        w.suspend([f"legacy:{old.id}"])  # suspended already: nothing more
        w.finish()
        self.assertEqual(w.suspended, 2)
        suspended = set(self.col.find_cards("is:suspended"))
        self.assertEqual(
            suspended,
            set(self.col.find_cards(f"nid:{old.id}"))
            | set(self.col.find_cards(f"tag:{self.kc.kn.note_key_tag(self.KEY)}")),
        )
        self.assertEqual(self.col.undo_status().undo, self.kc.UNDO_NAME)
        self.col.undo()  # one step: the new note and the suspensions
        self.assertEqual(self.col.find_cards("is:suspended"), [])
        self.assertEqual(self.kc.count_notes(self.col), 2)

    def test_suspending_alone_is_an_undo_step(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec()], "K")
        w = self.kc.RunWriter(self.col, "K")
        w.suspend(["puzzle:9:not-in-the-collection"])
        self.assertFalse(w.touched)  # nothing to suspend: no undo step
        w.suspend([self.KEY])
        w.finish()
        self.assertEqual((w.touched, w.suspended), (True, 1))
        self.col.undo()
        self.assertEqual(self.col.find_cards("is:suspended"), [])

    def test_existing_notes_lists_puzzles_by_key_and_older_notes_as_legacy(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec()], "Chess::Knightly")
        old = self.col.new_note(self.col.models.by_name(self.kc.MODEL))
        old["PGN"], old["Cards"] = "1. d4 { [%kr 1] } d5 *", "{{c1::1::1}}"
        old.add_tag("knightly")
        old.add_tag("knightly::game::lichess-old1::opening")  # made by an earlier version
        self.col.add_note(old, self.col.decks.id("Chess::Knightly"))
        found = self.kc.existing_notes(self.col)
        self.assertEqual(found[self.KEY], "1. e4 { [%kr 1] } e5 *")
        self.assertEqual(found[f"legacy:{old.id}"], "1. d4 { [%kr 1] } d5 *")
        self.assertEqual(len(found), 2)

    def test_key_from_tags(self):
        self.assertEqual(self.kc.key_from_tags(["x", self.kc.kn.note_key_tag(self.KEY)]), self.KEY)
        self.assertEqual(self.kc.kn.note_key_tag(self.KEY), "knightly::puzzle::005-abcdef123456")
        self.assertIsNone(self.kc.key_from_tags(["knightly"]))
        self.assertIsNone(self.kc.key_from_tags(["knightly::game::chesscom-123::endgame"]))  # an older version's


if __name__ == "__main__":
    unittest.main()

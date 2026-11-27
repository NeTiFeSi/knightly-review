"""Note type, media and note writes against a throwaway collection (needs the `anki` package and a dev build)."""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest

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

        from knightly_review import _save_visits

        self.kc.ensure_installed(self.col, self.card_dir)
        mm = self.col.models
        nt = mm.by_name(self.kc.MODEL)
        mm.remove_field(nt, next(f for f in nt["flds"] if f["name"] == "Visits"))  # an older note type
        mm.update_dict(nt)
        self.assertIn("Visits field added", self.kc.ensure_installed(self.col, self.card_dir))
        self.kc.apply_notes(self.col, [self.spec()], "KR")
        note = self.col.get_note(self.col.find_notes('"note:Knightly Review"')[0])
        card = SimpleNamespace(note=lambda: self.col.get_note(note.id), col=self.col)
        self.assertEqual(_save_visits(card, 'k1:1|{"r1":2,"r2":1}'), {"ok": True})
        self.assertEqual(_save_visits(card, 'k1:1|{"r1":1,"r3":4}'), {"ok": True})  # the larger count per position wins
        self.assertEqual(_save_visits(card, 'k2:2|{"r1":9}'), {"ok": True})  # another card of the note
        self.assertEqual(_save_visits(card, 'k2:2|{"r1":9}'), {"ok": True, "same": True})
        stored = json.loads(self.col.get_note(note.id)["Visits"])
        self.assertEqual(stored, {"k1:1": {"r1": 2, "r2": 1, "r3": 4}, "k2:2": {"r1": 9}})

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

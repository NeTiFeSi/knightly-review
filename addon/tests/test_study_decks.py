"""Study decks (filtered) against a throwaway collection (needs the `anki` package and a dev build)."""

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

HAVE_BUILD = (ADDON / "knightly_review" / "vendor" / "knightly").is_dir() and (ADDON / "knightly_review" / "card").is_dir()


@unittest.skipUnless(Collection and HAVE_BUILD, "needs `anki` and `python3 build.py --dev`")
class StudyDecksTest(unittest.TestCase):
    def setUp(self):
        from knightly_review import collection as kc
        from knightly_review import study_decks as sd

        self.kc, self.sd = kc, sd
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.col = Collection(str(self.dir / "c.anki2"))
        self.addCleanup(self.col.close)
        card_dir = self.dir / "card"
        shutil.copytree(ADDON / "knightly_review" / "card", card_dir)
        kc.ensure_installed(self.col, card_dir)
        self.n = 0

    def add(self, *tags, deck="Knightly Review"):
        from knightly_review.vendor.knightly import notes as kn
        from knightly_review.vendor.knightly.model import NoteSpec

        self.n += 1
        key = f"lichess:g{self.n:04d}:endgame"
        spec = NoteSpec(key=key, pgn="1. e4 { [%kr 1] } e5 *", cards="{{c1::1::1}}", notes="n",
                        tags=["knightly", kn.note_key_tag(key), *tags], card_ids={})
        self.kc.apply_notes(self.col, [spec], deck)

    def names(self):
        return {d.name for d in self.col.decks.all_names_and_ids() if d.name.startswith("Knightly Review::")}

    def test_creates_matching_decks_only(self):
        self.add("knightly::severity::blunder", "knightly::phase::endgame", "knightly::motif::back-rank-mate")
        created = self.sd.sync(self.col, "Knightly Review", min_cards=1)
        self.assertEqual(self.names(), {f"Knightly Review::{n}" for n in (
            "Severity - Blunders", "Phase - Endgame", "Mate only", "Random mix", "Motif - Back rank mate")})
        self.assertIn("Severity - Blunders", created)
        # created empty: the card stays in the main deck
        main = self.col.decks.id_for_name("Knightly Review")
        self.assertEqual(self.col.decks.card_count([main], include_subdecks=False), 1)
        # rebuilding one moves the card; updates still find the note by tag
        self.col.sched.rebuild_filtered_deck(self.col.decks.id_for_name("Knightly Review::Mate only"))
        self.assertEqual(len(self.col.find_cards('"deck:Knightly Review::Mate only"')), 1)
        self.assertEqual(self.kc.count_notes(self.col), 1)
        self.assertEqual(self.sd.sync(self.col, "Knightly Review", min_cards=1), [])

    def test_idempotent_and_min_cards(self):
        self.add("knightly::motif::fork")
        self.sd.sync(self.col, "Knightly Review", min_cards=2)
        self.assertNotIn("Knightly Review::Motif - Fork", self.names())
        self.add("knightly::motif::fork")
        self.sd.sync(self.col, "Knightly Review", min_cards=2)
        first = self.names()
        self.assertIn("Knightly Review::Motif - Fork", first)
        self.sd.sync(self.col, "Knightly Review", min_cards=2)
        self.assertEqual(self.names(), first)

    def test_stale_deck_removed_and_user_deck_kept(self):
        self.add("knightly::phase::endgame")
        self.col.decks.id("Knightly Review::Phase - Opening")  # a normal deck with a preset's name
        self.sd.sync(self.col, "Knightly Review")
        did = self.col.decks.id_for_name("Knightly Review::Phase - Opening")
        self.assertFalse(self.col.decks.get(did)["dyn"])
        self.col.decks.id("Knightly Review::My own")
        self.sd.sync(self.col, "Knightly Review")
        self.assertIn("Knightly Review::My own", self.names())


if __name__ == "__main__":
    unittest.main()

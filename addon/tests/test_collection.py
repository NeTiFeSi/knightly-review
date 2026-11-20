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

HAVE_BUILD = (ADDON / "knightly_review" / "vendor" / "knightly").is_dir() and (ADDON / "knightly_review" / "card").is_dir()


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

    def spec(self, key="lichess:abcd1234", cards="{{c1::1::1}}", pgn="1. e4 { [%kr 1] } e5 *"):
        from knightly_review.vendor.knightly import notes as kn
        from knightly_review.vendor.knightly.model import NoteSpec

        return NoteSpec(key=key, pgn=pgn, cards=cards, notes="n",
                        tags=["knightly", kn.note_key_tag(key), "knightly::opening"], card_ids={})

    def test_install_is_idempotent_and_keeps_config(self):
        self.assertEqual(self.kc.ensure_installed(self.col, self.card_dir),
                         ["_knightly.js updated", "_kr_stockfish.js updated", "_kr_stockfish.wasm updated", "note type created"])
        self.assertEqual(self.kc.ensure_installed(self.col, self.card_dir), [])
        nt = self.col.models.by_name(self.kc.MODEL)
        self.assertEqual([f["name"] for f in nt["flds"]], ["PGN", "Cards", "Notes"])
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

    def test_apply_adds_then_updates(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        self.kc.apply_notes(self.col, [self.spec()], "Chess::Knightly")
        self.assertEqual(self.col.card_count(), 1)
        self.assertEqual(self.kc.existing_notes(self.col), {"lichess:abcd1234": "1. e4 { [%kr 1] } e5 *"})
        two = self.spec(cards="{{c1::1::1}} {{c2::2::2}}", pgn="1. e4 { [%kr 1] } e5 { [%kr 2] } *")
        self.kc.apply_notes(self.col, [two], "Chess::Knightly")
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(self.col.card_count(), 2)
        self.assertIn("Chess::Knightly", [d.name for d in self.col.decks.all_names_and_ids()])
        self.assertEqual(self.kc.count_notes(self.col), 1)

    def test_opening_notes_and_reposition(self):
        self.kc.ensure_installed(self.col, self.card_dir)
        game = self.spec(key="lichess:old1")
        self.kc.apply_notes(self.col, [game], "Chess::Knightly")
        a, b = "opening:black:french-defense:2", "opening:black:sicilian-defense:4"
        two = "{{c1::1::1}} {{c2::2::2}}"
        pgn2 = "1. e4 { [%kr 1] } e5 { [%kr 2] } *"
        self.kc.apply_notes(self.col, [self.spec(key=a, cards=two, pgn=pgn2), self.spec(key=b)], "Chess::Knightly")
        self.assertEqual(set(self.kc.existing_notes(self.col)), {"lichess:old1", a, b})

        def queue():
            cids = self.col.find_cards('"note:Knightly Review" is:new')
            cards = sorted((self.col.get_card(c) for c in cids), key=lambda c: c.due)
            return [(self.kc.key_from_tags(c.note().tags), c.ord) for c in cards]

        # The Sicilian became the more important opening: its cards go first, then the French note's in
        # cloze order; the game note's card is shifted after them.
        self.kc.apply_notes(self.col, [], "Chess::Knightly", [b, a])
        self.assertEqual(queue(), [(b, 0), (a, 0), (a, 1), ("lichess:old1", 0)])
        # Studied cards are left alone.
        first_a = next(c for c in self.col.find_cards(f'"tag:{self.kc.kn.note_key_tag(a)}"')
                       if self.col.get_card(c).ord == 0)
        card = self.col.get_card(first_a)
        card.type = card.queue = 1
        self.col.update_card(card)
        self.assertEqual(self.kc.reposition_new(self.col, [a, b]), 2)
        self.assertEqual(queue(), [(a, 1), (b, 0), ("lichess:old1", 0)])

    def test_key_from_tags(self):
        self.assertEqual(self.kc.key_from_tags(["x", "knightly::game::chesscom-123"]), "chesscom:123")
        self.assertIsNone(self.kc.key_from_tags(["knightly"]))
        self.assertEqual(self.kc.key_from_tags(["knightly::by-opening::white-kings-pawn-game::ply-007"]),
                         "opening:white:kings-pawn-game:7")


if __name__ == "__main__":
    unittest.main()

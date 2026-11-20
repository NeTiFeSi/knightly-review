import unittest

import chess

from knightly import motifs, notes
from knightly.jobs import _motif_note
from knightly.select import MOTIF_NOTE_CARDS, motif_note_key, parse_motif_note_key


def found(fen, *line, **kw):
    return motifs.detect(chess.Board(fen), list(line), **kw)


class MotifTest(unittest.TestCase):
    def test_knight_fork(self):
        # Nc7+ forks the king on e8 and the rook on a8.
        self.assertEqual(found("r3k3/8/8/3N4/8/8/8/4K3 w - - 0 1", "d5c7")[0], "fork")

    def test_back_rank_mate(self):
        fen = "6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1"
        got = found(fen, "a1a8", "g8f8" if False else "g8h8")  # the reply is irrelevant: a1a8 is mate
        self.assertEqual(got[0], "back-rank-mate")
        self.assertIn("mate-in-1", got)

    def test_mate_in_n_from_score(self):
        self.assertIn("mate-in-2", found("4k3/8/8/8/8/8/8/4K2R w - - 0 1", "h1h7", mate=2))

    def test_promotion(self):
        self.assertIn("promotion", found("8/P7/8/8/8/8/k7/4K3 w - - 0 1", "a7a8q"))

    def test_hanging_piece(self):
        self.assertEqual(found("4k3/8/8/3n4/8/8/8/3RK3 w - - 0 1", "d1d5")[0], "hanging-piece")

    def test_pin(self):
        # Bb5 pins the knight on c6 to the king on e8.
        self.assertIn("pin", found("4k3/8/2n5/8/8/3B4/8/4K3 w - - 0 1", "d3b5"))

    def test_quiet_and_other(self):
        self.assertEqual(found("4k3/8/8/8/8/8/8/4K2R w - - 0 1", "e1d1"), ["quiet-move"])
        self.assertEqual(motifs.detect(chess.Board(), []), ["other"])

    def test_sacrifice_flag(self):
        self.assertEqual(found("4k3/8/8/8/8/8/8/4K2R w - - 0 1", "e1d1", sacrifice=True), ["sacrifice"])


class MotifNoteTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(parse_motif_note_key(motif_note_key("fork", 3)), ("fork", 3))
        self.assertIsNone(parse_motif_note_key("opening:black:x:3"))
        for key in ("motif:fork:1", "motif:back-rank-mate:12", "motif:mate-in-2:1"):
            self.assertEqual(notes.key_from_tags(["knightly", notes.note_key_tag(key)]), key)
        self.assertEqual(notes.note_key_tag("motif:fork:2"), "knightly::by-motif::fork::n-002")

    def test_note_choice(self):
        full = {motif_note_key("fork", 1): {str(i): i for i in range(MOTIF_NOTE_CARDS)}}
        existing = {"motif:fork:1": "", "motif:pin:1": ""}
        self.assertEqual(_motif_note("fork", existing, full, {}), "motif:fork:2")
        self.assertEqual(_motif_note("pin", existing, full, {}), "motif:pin:1")
        self.assertEqual(_motif_note("skewer", existing, full, {}), "motif:skewer:1")
        filling = {"motif:pin:1": [None] * MOTIF_NOTE_CARDS}
        self.assertEqual(_motif_note("pin", existing, {}, filling), "motif:pin:2")


if __name__ == "__main__":
    unittest.main()

import unittest

import chess

from knightly import motifs, notes
from knightly.jobs import _motif_note
from knightly.select import MOTIF_NOTE_CARDS, motif_note_key, parse_motif_note_key


def found(fen, previous, *line, **kw):
    """Motifs of the puzzle after `previous` (the opponent's move from `fen`), solved by `line`."""
    board = chess.Board(fen)
    board.push_uci(previous)
    return motifs.detect(board, list(line), **kw)


class MotifTest(unittest.TestCase):
    def test_knight_fork(self):
        # Nc7+ forks the king and the rook on a8; the knight takes it next.
        got = found("r2k4/8/8/3N4/8/8/8/4K3 b - - 0 1", "d8e8", "d5c7", "e8d7", "c7a8")
        self.assertEqual(got[0], "fork")

    def test_back_rank_mate(self):
        got = found("6k1/5ppp/8/8/5n2/8/5PPP/R5K1 b - - 0 1", "f4h5", "a1a8")
        self.assertEqual(got[:2], ["back-rank-mate", "mate-in-1"])

    def test_promotion(self):
        self.assertIn("promotion", found("8/P7/8/8/8/8/k7/4K3 b - - 0 1", "a2a1", "a7a8q"))

    def test_no_previous_move(self):
        self.assertEqual(motifs.detect(chess.Board(), ["e2e4"]), ["other"])
        self.assertEqual(motifs.detect(chess.Board(), []), ["other"])

    def test_sacrifice_flag(self):
        self.assertIn("sacrifice", found("4k3/8/8/8/8/8/8/4K2R b - - 0 1", "e8d8", "h1h8", sacrifice=True))

    def test_slugs(self):
        self.assertEqual([motifs.slug(t) for t in ("backRankMate", "mateIn2", "xRayAttack", "fork")],
                         ["back-rank-mate", "mate-in-2", "x-ray-attack", "fork"])


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

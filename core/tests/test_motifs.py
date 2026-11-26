import unittest

import chess

from knightly import motifs


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


if __name__ == "__main__":
    unittest.main()

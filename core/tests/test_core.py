from __future__ import annotations

import io
import json
import multiprocessing
import os
import re
import stat
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

import chess
import chess.pgn
from helpers import (
    FIXTURES,
    FRENCH,
    PUZZLE_FEN,
    SCHOLAR_MOVES,
    SCHOLAR_SCORES,
    SICILIAN,
    WHITE_MATES,
    FakeAnalyser,
    FakeAnalysis,
    FakeResponse,
    FakeSession,
    S,
    ScriptedAnalyser,
    board_at,
    game_from,
    scholar_game,
    score_for,
    sf_info,
)
from knightly import (
    http,
    notes,
    openings,
    phases,
    pipeline,
    progress,
    puzzles,
    similar,
    sources,
    stats,
    statsview,
)
from knightly.cache import SqliteCache, fen_key
from knightly.config import Config
from knightly.engine import discover, install
from knightly.jobs import run_update
from knightly.model import Eval, Game, Line, NoWdl, Score
from knightly.progress import Tracker
from knightly.select import (
    BLUNDER,
    BRILLIANT,
    DUBIOUS,
    GOOD,
    INTERESTING,
    MISTAKE,
    NEUTRAL,
    SINGULAR,
    Graded,
    build_card,
    build_punish,
    grade,
    line_material,
)
from knightly.sources import chesscom, lichess

GOLDEN = FIXTURES / "generated_note.pgn"
GOLDEN_SUB = FIXTURES / "generated_sub_note.pgn"  # a note made from a position inside a mistake's tree


def division_games() -> list[tuple[str, list[str], tuple]]:
    """Finished Lichess games with the division Lichess gave them: (id, UCI moves, (middle, end))."""
    out = []
    for d in json.loads((FIXTURES / "lichess_divisions.json").read_text()):
        b, uci = chess.Board(), []
        for san in d["moves"].split():
            m = b.parse_san(san)
            uci.append(m.uci())
            b.push(m)
        out.append((d["id"], uci, tuple(d["division"])))
    return out


def tmpcache(test: unittest.TestCase) -> SqliteCache:
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    c = SqliteCache(Path(d.name) / "c.sqlite")
    test.addCleanup(c.close)
    return c


class ModelTest(unittest.TestCase):
    def test_win_pct_is_wdl_expectation(self):
        s = S(36, 73, 923, 4)  # Stockfish 19, start position
        self.assertAlmostEqual(s.win_pct(), 53.45)
        self.assertAlmostEqual(s.win_pct(False), 46.55)
        self.assertEqual(Score(mate=2, wdl=(1000, 0, 0)).win_pct(False), 0)
        with self.assertRaises(NoWdl):
            Score(cp=50).win_pct()

    def test_eval_text(self):
        self.assertEqual(Score(cp=17).pgn(), "0.17")
        self.assertEqual(Score(cp=-150).pgn(), "-1.50")
        self.assertEqual(Score(mate=-3).pgn(), "#-3")

    def test_json_roundtrips(self):
        g = scholar_game()
        self.assertEqual(Game.from_json(json.loads(json.dumps(g.to_json()))), g)
        self.assertEqual(Game.from_json({**g.to_json(), "site_evals": [{"cp": 1}]}), g)  # old cache rows
        s = S(-20, 55, 850, 95)
        self.assertEqual(Score.from_json(json.loads(json.dumps(s.to_json()))), s)

    def test_user_result(self):
        self.assertEqual(scholar_game().user_result(), 0)
        self.assertEqual(scholar_game(result="0-1").user_result(), 1)
        self.assertEqual(scholar_game(result="1/2-1/2").user_result(), 0.5)


def _writer(path: str, worker: int) -> None:
    c = SqliteCache(path)
    for i in range(150):
        c.put_eval(f"pos{worker}-{i}", "stockfish", 13, Eval(10, [Line(Score(cp=i), ["e2e4"])]))
        c.set(f"k{worker}-{i}", i)
    c.close()


class CacheTest(unittest.TestCase):
    def test_harder_search_wins_per_engine(self):
        c = tmpcache(self)
        c.put_eval("p", "stockfish", 20, Eval(20, [Line(S(10, 1, 998, 1), ["e2e4"])]))
        c.put_eval("p", "stockfish", 13, Eval(13, [Line(S(99, 1, 998, 1), ["d2d4"])]))
        self.assertEqual(c.get_eval("p", "stockfish", min_effort=13).score, S(10, 1, 998, 1))
        self.assertIsNone(c.get_eval("p", "stockfish", min_effort=21))
        self.assertIsNone(c.get_eval("p", "stockfish", multipv=2))
        self.assertIsNone(c.get_eval("p", "lc0"))  # engines never share entries
        c.put_eval("p", "lc0", 500, Eval(5, [Line(S(30, 200, 650, 150), ["d2d4"])]))
        self.assertEqual(c.get_eval("p", "lc0", min_effort=500).score, S(30, 200, 650, 150))
        self.assertIsNone(c.get_eval("p", "lc0", min_effort=1000))  # searched for less time than asked
        c.put_eval("p", "stockfish", 25, Eval(25, [Line(S(5, 1, 998, 1), ["e2e4"]), Line(S(1, 1, 998, 1), ["d2d4"])]))
        self.assertEqual(len(c.get_eval("p", "stockfish", multipv=2).lines), 2)
        self.assertEqual(len(c.get_eval("p", "stockfish", multipv=1).lines), 1)  # trimmed to what was asked
        # The deepest eval of each position, whatever its number of lines.
        c.put_eval("q", "stockfish", 1, Eval(1, [Line(S(0, 0, 1000, 0), ["e2e4"])]))
        best = c.best_evals(["p", "q", "none"], "stockfish")
        self.assertEqual({k: e for k, (e, _) in best.items()}, {"p": 25, "q": 1})
        self.assertEqual(best["p"][1].score, S(5, 1, 998, 1))

    def test_old_cache_without_wdl_is_dropped(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "old.sqlite"
        c = SqliteCache(path)
        g = scholar_game()
        c.put_games([g], "knightlyme")
        c.put_eval("p", "stockfish", 30, Eval(30, [Line(Score(cp=10), ["e2e4"])]))
        c.put_analysis(g, [{"cp": 1}])
        c.set("schema", 1)
        c.close()
        c = SqliteCache(path)
        self.addCleanup(c.close)
        self.assertIsNone(c.get_eval("p", "stockfish"))
        self.assertIsNone(c.get_analysis(g))
        self.assertEqual(c.games("lichess", "knightlyme"), [g])  # games are kept

    def test_games_and_analysis(self):
        c = tmpcache(self)
        g = scholar_game()
        self.assertEqual(c.put_games([g, g], "knightlyme"), 1)
        self.assertEqual(c.games("lichess", "KnightlyMe"), [g])
        self.assertIsNone(c.get_analysis(g))
        c.put_analysis(g, [{"cp": 1}])
        self.assertEqual(c.get_analysis(g), [{"cp": 1}])

    def test_threads_and_processes(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = str(Path(d.name) / "shared.sqlite")
        SqliteCache(path).close()
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_writer, args=(path, w)) for w in range(2)]
        threads = [threading.Thread(target=_writer, args=(path, w)) for w in range(2, 4)]
        for p in procs + threads:
            p.start()
        for p in procs + threads:
            p.join(60)
        self.assertTrue(all(p.exitcode == 0 for p in procs))
        c = SqliteCache(path)
        self.addCleanup(c.close)
        for w in range(4):
            self.assertEqual(c.get(f"k{w}-149"), 149)
            self.assertEqual(c.get_eval(f"pos{w}-7", "stockfish").score.cp, 7)


class HttpTest(unittest.TestCase):
    def test_waits_on_429_then_succeeds(self):
        s = FakeSession([("x", [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, b"ok")])])
        waits = []
        r = http.get(s, "https://x", on_wait=waits.append)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(waits, [0.0])

    def test_gives_up(self):
        s = FakeSession([("x", FakeResponse(429, headers={"Retry-After": "0"}))])
        with self.assertRaises(http.RateLimited):
            http.get(s, "https://x", retries=1)

    def test_cancel_during_wait(self):
        s = FakeSession([("x", FakeResponse(429))])
        ev = threading.Event()
        ev.set()
        with self.assertRaises(http.Cancelled):
            http.get(s, "https://x", cancel=ev)


class LichessTest(unittest.TestCase):
    def setUp(self):
        self.lines = (FIXTURES / "lichess_games.ndjson").read_text().splitlines()

    def test_parse(self):
        g = lichess.parse_game(json.loads(self.lines[0]), "knightlyme")
        self.assertEqual((g.source, g.id), ("lichess", "game0000"))
        self.assertFalse(g.user_is_white)
        self.assertTrue(g.url.endswith("/black"))
        self.assertEqual(g.result, "0-1")
        self.assertEqual(g.moves[:2], ["e2e4", "c7c5"])
        self.assertIsNone(lichess.parse_game(json.loads(self.lines[0]), "someoneelse"))
        self.assertIsNone(lichess.parse_game({**json.loads(self.lines[0]), "variant": "chess960"}, "knightlyme"))

    def test_fetch_is_incremental(self):
        c = tmpcache(self)
        body = ("\n".join(self.lines) + "\n").encode()
        s = FakeSession([("/api/games/user/", FakeResponse(200, body))])
        self.assertEqual(lichess.fetch(c, "KnightlyMe", session=s, time_classes=["blitz", "rapid"]), 2)
        params = s.requests[0][1]["params"]
        self.assertEqual(params["perfType"], "blitz,rapid")
        self.assertNotIn("evals", params)  # Lichess's evals have no WDL; the local engine does all analysis
        newest = max(json.loads(l)["createdAt"] for l in self.lines)
        self.assertEqual(c.get("cursor:lichess:knightlyme"), newest + 1)
        self.assertEqual(lichess.fetch(c, "KnightlyMe", session=s), 0)  # already stored
        self.assertEqual(s.requests[1][1]["params"]["since"], newest + 1)

    def test_all_time_and_growing_look_back(self):
        c = tmpcache(self)
        body = ("\n".join(self.lines) + "\n").encode()
        s = FakeSession([("/api/games/user/", FakeResponse(200, body))])
        self.assertTrue(lichess.needs_backfill(c, "KnightlyMe", 90))  # nothing fetched yet
        lichess.fetch(c, "KnightlyMe", session=s, history_days=30)
        self.assertGreater(s.requests[0][1]["params"]["since"], 0)
        self.assertNotIn("max", s.requests[0][1]["params"])
        self.assertFalse(lichess.needs_backfill(c, "KnightlyMe", 30))
        self.assertFalse(lichess.needs_backfill(c, "KnightlyMe", 10))
        self.assertTrue(lichess.needs_backfill(c, "KnightlyMe", 0))
        # All time: starts over from the very beginning, then is incremental again.
        lichess.fetch(c, "KnightlyMe", session=s, history_days=0)
        self.assertEqual(s.requests[1][1]["params"]["since"], 0)
        self.assertFalse(lichess.needs_backfill(c, "KnightlyMe", -1))
        lichess.fetch(c, "KnightlyMe", session=s, history_days=0)
        self.assertGreater(s.requests[2][1]["params"]["since"], 0)


class ChesscomTest(unittest.TestCase):
    def test_parse_and_fetch_with_etag(self):
        month = json.loads((FIXTURES / "chesscom_2026_10.json").read_text())
        g = chesscom.parse_game(month["games"][0], "knightlyme")
        self.assertEqual((g.source, g.time_class, g.user_is_white), ("chesscom", "correspondence", False))
        self.assertIsNone(chesscom.parse_game(month["games"][1], "knightlyme"))  # chess960
        c = tmpcache(self)
        arch = {
            "archives": [
                "https://api.chess.com/pub/player/knightlyme/games/2026/09",
                "https://api.chess.com/pub/player/knightlyme/games/2026/10",
            ]
        }
        s = FakeSession(
            [
                ("/games/archives", FakeResponse(200, arch)),
                ("/games/2026/10", [FakeResponse(200, month, {"ETag": 'W/"abc"'}), FakeResponse(304)]),
                ("/games/2026/09", FakeResponse(200, {"games": []})),
            ]
        )
        now = 1793000000  # 2026-10-26: October is the current month
        self.assertEqual(chesscom.fetch(c, "KnightlyMe", session=s, history_days=60, now=now), 2)
        self.assertEqual(chesscom.fetch(c, "KnightlyMe", session=s, history_days=60, now=now), 0)
        oct_reqs = [kw for url, kw in s.requests if url.endswith("2026/10")]
        self.assertEqual(oct_reqs[1]["headers"]["If-None-Match"], 'W/"abc"')
        self.assertEqual(sum(url.endswith("2026/09") for url, _ in s.requests), 1)  # finished month: once

    def test_all_time_backfill(self):
        c = tmpcache(self)
        base = "https://api.chess.com/pub/player/knightlyme/games/"
        arch = {"archives": [base + "2019/03", base + "2026/09", base + "2026/10"]}
        s = FakeSession(
            [
                ("/games/archives", FakeResponse(200, arch)),
                ("/games/", FakeResponse(200, {"games": []})),
            ]
        )
        now = 1793000000  # 2026-10-26
        months = lambda: [u.rsplit("games/", 1)[1] for u, _ in s.requests if "/games/2" in u]
        chesscom.fetch(c, "KnightlyMe", session=s, history_days=30, now=now)
        self.assertEqual(months(), ["2026/09", "2026/10"])
        self.assertFalse(chesscom.needs_backfill(c, "KnightlyMe", 30, now=now))
        self.assertTrue(chesscom.needs_backfill(c, "KnightlyMe", 0, now=now))
        s.requests.clear()
        chesscom.fetch(c, "KnightlyMe", session=s, history_days=0, now=now)
        self.assertEqual(months(), ["2019/03", "2026/10"])  # September is done; October is current
        self.assertFalse(chesscom.needs_backfill(c, "KnightlyMe", 0, now=now))

    def test_cooldown(self):
        c = tmpcache(self)
        self.assertTrue(sources.due(c, "chesscom", "u", now=1000))
        sources.mark_fetched(c, "chesscom", "u", now=1000)
        self.assertFalse(sources.due(c, "chesscom", "u", now=1000 + 3600))
        self.assertTrue(sources.due(c, "chesscom", "u", now=1000 + 3 * 3600))


class SelectTest(unittest.TestCase):
    cfg = Config()

    def g(self, moves, legal=20, current=0, **cfg):
        """Grade (move, loss, material) triples; returns {move: nag}."""
        c = Config(**cfg)
        return {m.move: m.nag for m in grade([Graded(*t) for t in moves], legal, c, current)}

    def test_brilliant_and_singular(self):
        # Only one good move; it gives up 3 points that another (bad) line keeps.
        self.assertEqual(self.g([("a", 0, -3), ("b", 30, 0)]), {"a": BRILLIANT, "b": BLUNDER})
        self.assertEqual(self.g([("a", 0, 0), ("b", 30, 0)]), {"a": SINGULAR, "b": BLUNDER})
        self.assertEqual(self.g([("a", 0, -1), ("b", 30, 0)]), {"a": SINGULAR, "b": BLUNDER})  # a pawn isn't enough
        self.assertEqual(self.g([("a", 0, -3)], legal=1), {"a": GOOD})  # forced: nothing to find
        # Winning a piece isn't a sacrifice, even if a (losing) line grabs even more.
        self.assertEqual(
            self.g([("takes_knight", 0, 3), ("takes_queen", 50, 9)]), {"takes_knight": SINGULAR, "takes_queen": BLUNDER}
        )
        # Giving back less than the material you're up still counts against the current balance.
        self.assertEqual(self.g([("a", 0, 2), ("b", 30, 5)], current=5), {"a": BRILLIANT, "b": BLUNDER})

    def test_interesting_and_good(self):
        nags = self.g([("sac", 0, -3), ("safe", 0.8, 0), ("bad", 8, 0)])
        self.assertEqual(nags, {"sac": INTERESTING, "safe": GOOD, "bad": DUBIOUS})
        # Two good moves that sacrifice the same: neither has a good alternative keeping more material.
        self.assertEqual(
            self.g([("x", 0, -3), ("y", 1, -3), ("greedy", 25, 0)]), {"x": GOOD, "y": GOOD, "greedy": BLUNDER}
        )

    def test_bad_move_bands_and_threshold(self):
        nags = self.g([("best", 0, 0), ("a", 5, 0), ("b", 10, 0), ("c", 10.5, 0), ("d", 20, 0), ("e", 21, 0)])
        self.assertEqual([nags[k] for k in "abcde"], [DUBIOUS, DUBIOUS, MISTAKE, MISTAKE, BLUNDER])
        self.assertEqual(self.g([("best", 0, 0), ("a", 0.9, 0)])["a"], GOOD)
        self.assertEqual(self.g([("best", 0, 0), ("a", 1.5, 0)])["a"], NEUTRAL)
        self.assertEqual(self.g([("best", 0, 0), ("a", 4.9, 0)])["a"], NEUTRAL)
        self.assertEqual(self.g([("best", 0, 0), ("a", 1.5, 0)], good_move=2.0)["a"], GOOD)
        self.assertEqual(self.g([("best", 0, 0), ("a", 3, 0)], inaccuracy=2.0)["a"], DUBIOUS)

    def test_forced_mate_needs_a_mating_move(self):
        def g(moves, legal=20):
            return {
                m.move: m.nag
                for m in grade([Graded(mv, loss, 0, mate=mate) for mv, loss, mate in moves], legal, self.cfg)
            }

        # Any forced mate is good, however long; a won position without the mate is not.
        self.assertEqual(
            g([("m3", 0, 3), ("m5", 0, 5), ("won", 0, None), ("worse", 15, None)]),
            {"m3": GOOD, "m5": GOOD, "won": DUBIOUS, "worse": MISTAKE},
        )
        # The only move that keeps the mate is the singular move.
        self.assertEqual(g([("m2", 0, 2), ("won", 0, None)]), {"m2": SINGULAR, "won": DUBIOUS})
        # Being mated isn't a mate to keep: plain WDL grading.
        self.assertEqual(g([("a", 0, -3), ("b", 0, -2)]), {"a": GOOD, "b": GOOD})

    def test_line_material(self):
        b = chess.Board()
        # Counted at 4 plies, but Nxf7 is a capture in progress: follow it through Kxf7.
        self.assertEqual(line_material(b, ["g1f3", "e7e5", "f3e5", "d7d6", "e5f7", "e8f7"], 4), -1)
        # The recapture finishes the exchange; Nc3 is quiet.
        self.assertEqual(line_material(b, ["e2e4", "d7d5", "e4d5", "d8d5", "b1c3", "d5a5"], 4), 0)
        # A short line is continued by the engine when given one.
        short = chess.Board("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1")
        table = {
            fen_key(chess.Board("4k3/8/8/3P4/8/8/8/4K3 b - - 0 1").fen()): Eval(
                20, [Line(S(0, 0, 1000, 0), ["e8d7", "e1e2", "d7d6", "e2d3"])]
            )
        }
        self.assertEqual(line_material(short, ["e4d5"], 4, FakeAnalyser(table=table)), 1)

    def test_line_material_in_between_move(self):
        # Berlin-style 5.Re1 Nd6 6.Nxe5 Nxe5 7.Bf1 Be7 8.Rxe5: Bf1 is an in-between move, Rxe5 is still owed.
        b = chess.Board("r1bqkb1r/pppp1ppp/2n5/1B2p3/4n3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 0 5")
        line = ["f1e1", "e4d6", "f3e5", "c6e5", "b5f1", "f8e7", "e1e5", "e8g8"]
        self.assertEqual(line_material(b, line, 4), 0)
        # 5.Nxe5 Nxe5 6.d4: nothing to recapture with yet, the knight really is given up for a while.
        b = chess.Board("r1bqk2r/ppppnppp/2n5/1Bb1p3/4P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 6 5")
        line = ["f3e5", "c6e5", "d2d4", "c7c6", "b5e2", "c5d6", "d4e5", "d6e5"]
        self.assertEqual(line_material(b, line, 4), -2)

    def test_build_card(self):
        card = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(), self.cfg)
        self.assertEqual(card.played.nag, BLUNDER)
        self.assertEqual([a.move for a in card.alternatives], ["g7g6", "d8e7", "d8f6"])
        self.assertEqual([a.nag for a in card.alternatives], [GOOD, GOOD, GOOD])  # three good moves, no sacrifice
        self.assertIn("g6 was best", card.comment)
        self.assertIn("loses 52% win rate", card.comment)

    def test_build_card_a_small_loss_still_makes_a_puzzle(self):
        # The played move loses 2 %: not a good move (more than 1 % worse), so a puzzle; no NAG (normal play).
        table = {
            PUZZLE_FEN: Eval(
                20,
                [
                    Line(S(-30, 50, 850, 100), ["g7g6"]),
                    Line(S(-25, 70, 850, 80), ["g8f6"]),
                    Line(S(500, 900, 100, 0), ["a7a6"]),
                ],
            )
        }
        card = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(table=table), self.cfg)
        self.assertEqual((card.played.nag, round(card.loss)), (NEUTRAL, 2))

    def test_build_card_a_good_move_is_no_puzzle(self):
        table = {PUZZLE_FEN: Eval(20, [Line(S(-30, 50, 850, 100), ["g7g6"]), Line(S(-29, 55, 850, 95), ["g8f6"])])}
        self.assertIsNone(build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(table=table), self.cfg))

    def test_build_card_min_loss(self):
        table = {
            PUZZLE_FEN: Eval(
                20,
                [
                    Line(S(-30, 50, 850, 100), ["g7g6"]),
                    Line(S(-25, 70, 850, 80), ["g8f6"]),
                    Line(S(500, 900, 100, 0), ["a7a6"]),
                ],
            )
        }
        self.assertIsNone(
            build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(table=table), Config(min_loss=5))
        )
        self.assertEqual(Config(min_loss=3).min_loss_at(17), 3)

    def test_build_punish(self):
        after = board_at(SCHOLAR_MOVES, 6)  # Black's Nf6?? allows Qxf7#
        table = {fen_key(after.fen()): Eval(20, [Line(WHITE_MATES, ["h5f7"]), Line(S(300, 700, 300, 0), ["h5h4"])])}
        card = build_punish(after, 6, "Nf6", FakeAnalyser(table=table), self.cfg)
        self.assertEqual((card.played.move, card.ply, card.fen_key), ("h5f7", 6, fen_key(after.fen())))
        self.assertEqual(card.comment, "Nf6 is punished by Qxf7#.")
        self.assertEqual([a.move for a in card.alternatives], ["h5h4"])
        mated = board_at(SCHOLAR_MOVES, 7)
        self.assertIsNone(build_punish(mated, 7, "Qxf7#", FakeAnalyser(), self.cfg))  # the game is over


class PhasesTest(unittest.TestCase):
    def test_matches_lichess(self):
        for gid, moves, division in division_games():
            self.assertEqual(phases.divide(moves), division, gid)

    def test_phase_at(self):
        self.assertEqual(phases.divide([]), (None, None))
        self.assertEqual(phases.divide(SCHOLAR_MOVES), (None, None))
        self.assertEqual(
            [phases.phase_at((16, 40), p) for p in (0, 15, 16, 39, 40)],
            ["opening", "opening", "middlegame", "middlegame", "endgame"],
        )
        self.assertEqual(phases.phase_at((None, None), 80), "opening")

    def test_endgame_needs_a_middlegame_before_it(self):
        start = chess.Board()
        bare = chess.Board("4k3/pppppppp/8/8/8/8/PPPPPPPP/4K3 w - - 0 1")  # no pieces: both at once
        self.assertEqual(phases.divide_boards([start, bare]), (None, 1))
        self.assertEqual(phases.phase_at((None, 1), 1), "endgame")


AFTER_NF6 = fen_key(board_at(SCHOLAR_MOVES, 6).fen())
# Black's Nf6?? is punished by Qxf7#; the other moves are not.
PUNISH_TABLE = {
    PUZZLE_FEN: __import__("helpers").PUZZLE_EVAL,
    AFTER_NF6: Eval(20, [Line(WHITE_MATES, ["h5f7"]), Line(S(300, 700, 300, 0), ["h5h4"])]),
}


class NotesTest(unittest.TestCase):
    def _note(self, notes_text="Opening"):
        g = scholar_game()
        an = FakeAnalyser(table=PUNISH_TABLE)
        first = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", an, Config())
        second = build_punish(board_at(SCHOLAR_MOVES, 6), 6, "Nf6", an, Config())
        return notes.build_puzzle(g, SCHOLAR_SCORES, first, second, notes_text)

    def test_golden_pgn(self):
        """The card UI's tests parse this file too (card/test/generated.test.ts)."""
        n = self._note()
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.write_text(n.pgn + "\n")
        self.assertEqual(n.pgn + "\n", GOLDEN.read_text())
        self.assertEqual(n.cards, "{{c1::1::1}} {{c2::2::2}}")
        self.assertEqual(n.key, puzzles.puzzle_key(PUZZLE_FEN, 5))
        self.assertEqual(n.new_cards, 2)
        # The tags describe the first puzzle only.
        self.assertEqual(
            n.tags,
            [
                "knightly",
                notes.note_key_tag(n.key),
                "knightly::color::black",
                "knightly::motif::other",
                "knightly::opening::king-s-pawn-game",
                "knightly::phase::opening",
                "knightly::rated",
                "knightly::result::loss",
                "knightly::severity::blunder",
                "knightly::site::lichess",
                "knightly::time::blitz",
            ],
        )
        self.assertNotIn("#0", n.pgn)
        self.assertNotIn("100.00", n.pgn)

    def test_two_puzzles_one_note(self):
        n = self._note()
        self.assertEqual(n.card_ids, {PUZZLE_FEN: 1, AFTER_NF6: 2})
        self.assertEqual(notes.card_ids(n.pgn), {PUZZLE_FEN: 1, AFTER_NF6: 2})
        self.assertEqual(n.pgn.count("[Event"), 1)  # one game: the punishment follows the mistake

    def test_one_puzzle_when_nothing_to_punish(self):
        first = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(table=PUNISH_TABLE), Config())
        n = notes.build_puzzle(scholar_game(), SCHOLAR_SCORES, first, None, "")
        self.assertEqual((n.cards, n.new_cards), ("{{c1::1::1}}", 1))

    def test_opponent_card_comment_and_tag(self):
        table = {
            fen_key(board_at(SCHOLAR_MOVES, 2).fen()): Eval(
                20, [Line(S(30, 100, 850, 50), ["g1f3", "b8c6"]), Line(S(-200, 0, 600, 400), ["d1h5", "b8c6"])]
            )
        }
        card = build_card(board_at(SCHOLAR_MOVES, 2), 2, "d1h5", FakeAnalyser(table=table), Config(), "Opponent")
        self.assertFalse(card.mine)
        self.assertTrue(card.comment.startswith("Opponent played Qh5 here, losing 22% win rate."))
        self.assertIn("knightly::opponent", notes.build_puzzle(scholar_game(), SCHOLAR_SCORES, card, None, "").tags)

    def test_key_and_tag_roundtrip(self):
        key = puzzles.puzzle_key(PUZZLE_FEN, 5)
        tag = notes.note_key_tag(key)
        self.assertTrue(tag.startswith("knightly::puzzle::005-"))
        self.assertEqual(notes.key_from_tags(["x", tag]), key)
        self.assertIsNone(notes.key_from_tags(["knightly", "knightly::game::lichess-1::opening"]))
        self.assertEqual(puzzles.parse_key(key)[0], 5)
        self.assertIsNone(puzzles.parse_key("lichess:abcd:opening"))

    def test_notes_field(self):
        text = notes.puzzle_notes(
            "Opening", ['<div class="kr-stats" data-card="1">x</div>', ""], ["Bd3 would also be a good move if x."]
        )
        self.assertEqual(
            text,
            'Opening<br><div class="kr-stats" data-card="1">x</div><br><b>Similar positions</b>'
            "<br>Bd3 would also be a good move if x.",
        )
        self.assertEqual(notes.puzzle_notes("Opening", [], []), "Opening")

    def test_field_roundtrip(self):
        pgn = self._note().pgn
        self.assertEqual(notes.field_to_pgn(notes.pgn_to_field(pgn)), pgn)


class OpeningsTest(unittest.TestCase):
    def test_classify_and_family(self):
        eco, name = openings.classify(SICILIAN)
        self.assertEqual((eco, name), ("B90", "Sicilian Defense: Najdorf Variation"))
        self.assertEqual(openings.family(name), "Sicilian Defense")
        # A transposition (1. Nf3 c5 2. e4) still lands in the table.
        self.assertEqual(openings.classify(["g1f3", "c7c5", "e2e4"])[1], "Sicilian Defense")
        self.assertEqual(openings.classify([]), ("", "Other"))


def seed(test, games, user="knightlyme"):
    c = tmpcache(test)
    c.put_games(games, user)
    return c


def recent(**kw):
    import time as _t

    kw.setdefault("end_ts", int(_t.time()) - 3600)
    return scholar_game(**kw)


CFG = {"lichess_user": "KnightlyMe", "stockfish_path": "x"}
# A second position like the scholar's (the bishop on b5 instead of c4), where Nf6 is also a mistake.
B_MOVES = ["e2e4", "e7e5", "d1h5", "b8c6", "f1b5", "g8f6"]
B_TABLE = {
    fen_key(board_at(B_MOVES, 5).fen()): Eval(
        20,
        [
            Line(S(-30, 50, 850, 100), ["g7g6", "h5f3"]),
            Line(S(-20, 55, 850, 95), ["d8e7", "b1c3"]),
            Line(S(-10, 60, 850, 90), ["d8f6", "b1c3"]),
            Line(S(400, 800, 200, 0), ["g8f6", "h5f7"]),
        ],
    )
}


class JobsTest(unittest.TestCase):
    def test_a_mistake_is_one_note_with_two_puzzles(self):
        c = seed(self, [recent()])
        res = run_update(Config(**CFG), c, fetch=False, analyser_factory=lambda *a: FakeAnalyser(table=PUNISH_TABLE))
        (note,) = res.notes
        self.assertEqual(
            (note.key, note.new_cards, note.cards), (puzzles.puzzle_key(PUZZLE_FEN, 5), 2, "{{c1::1::1}} {{c2::2::2}}")
        )
        self.assertEqual(res.new_cards, 2)
        self.assertGreater(res.analysed_positions, 0)
        self.assertTrue(note.notes.startswith("Opening · <a href"))
        # Second run: the position is on a card already, and nothing about the note changed.
        again = run_update(
            Config(**CFG),
            c,
            fetch=False,
            existing={note.key: note.pgn},
            analyser_factory=lambda *a: FakeAnalyser(table=PUNISH_TABLE),
        )
        self.assertEqual((again.new_cards, again.notes), (0, []))

    def test_no_second_puzzle_when_the_reply_is_unknown(self):
        res = run_update(Config(**CFG), seed(self, [recent()]), fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual([(n.new_cards, n.cards) for n in res.notes], [(1, "{{c1::1::1}}")])

    def test_old_cards_keep_their_positions_off_the_list(self):
        old = "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 { [%kr 1] } 4. Qxf7# *"
        res = run_update(
            Config(**CFG),
            seed(self, [recent()]),
            fetch=False,
            existing={"lichess:test0001:opening": old},
            analyser_factory=FakeAnalyser,
        )
        self.assertEqual(res.notes, [])

    def test_ply_by_ply_and_the_card_cap(self):
        now = int(__import__("time").time())
        sic = game_from(SICILIAN, white=False, id="s1", end_ts=now - 100)
        fr = game_from(FRENCH, white=False, id="f1", end_ts=now - 200)
        script = {"s1": {5: 30}, "f1": {1: 20}}
        an = lambda *a: ScriptedAnalyser([sic, fr], script)
        res = run_update(Config(**CFG, max_new_cards=5), seed(self, [sic, fr]), fetch=False, analyser_factory=an)
        # Ply 1 (the French game's) before ply 5, though the Sicilian game is newer.
        self.assertEqual([n.key.split(":")[1] for n in res.notes], ["1", "5"])
        one = run_update(Config(**CFG, max_new_cards=1), seed(self, [sic, fr]), fetch=False, analyser_factory=an)
        self.assertEqual([n.key.split(":")[1] for n in one.notes], ["1"])
        self.assertTrue(one.finished)  # enough puzzles

    def test_opponent_mistakes_come_at_their_ply(self):
        table = dict(PUNISH_TABLE)
        table[fen_key(board_at(SCHOLAR_MOVES, 2).fen())] = Eval(
            20, [Line(S(30, 100, 850, 50), ["g1f3", "b8c6"]), Line(S(-200, 0, 600, 400), ["d1h5", "b8c6"])]
        )
        an = lambda *a: FakeAnalyser(table=table)
        c = seed(self, [recent()])
        ours = run_update(Config(**CFG), c, fetch=False, analyser_factory=an)
        self.assertEqual([n.key.split(":")[1] for n in ours.notes], ["5"])
        both = run_update(
            Config(**CFG, include_opponent=True), seed(self, [recent()]), fetch=False, analyser_factory=an
        )
        self.assertEqual(
            [(n.key.split(":")[1], "knightly::opponent" in n.tags) for n in both.notes], [("2", True), ("5", False)]
        )

    def test_repeated_positions_update_the_notes_stats(self):
        a, b = recent(), recent(id="test0002", end_ts=int(__import__("time").time()) - 60)
        an = lambda *a_: FakeAnalyser(table=PUNISH_TABLE)
        c = seed(self, [a])
        first = run_update(Config(**CFG), c, fetch=False, analyser_factory=an)
        self.assertNotIn("Played before", first.notes[0].notes)
        c.put_games([b], "knightlyme")
        res = run_update(
            Config(**CFG), c, fetch=False, existing={first.notes[0].key: first.notes[0].pgn}, analyser_factory=an
        )
        (changed,) = res.notes
        self.assertEqual((changed.key, changed.new_cards, res.new_cards), (first.notes[0].key, 0, 0))
        self.assertIn("2 games reached this position · White 2 (100%)", changed.notes)
        self.assertIn('<div class="kr-stats" data-card="1">', changed.notes)
        self.assertIn("After the mistake: 2 games reached this position", changed.notes)
        self.assertEqual(changed.pgn, first.notes[0].pgn)
        self.assertEqual(changed.tags, first.notes[0].tags)

    def test_a_similar_position_comments_on_the_old_puzzle_instead_of_a_new_note(self):
        a = recent()
        b = recent(id="test0002", moves=list(B_MOVES), end_ts=int(__import__("time").time()) - 60)
        table = {**PUNISH_TABLE, **B_TABLE}

        def an(*_):
            f = FakeAnalyser(table=table)
            f.scores[fen_key(board_at(B_MOVES, 6).fen())] = S(400, 800, 200, 0)
            return f

        c = seed(self, [a])
        first = run_update(Config(**CFG), c, fetch=False, analyser_factory=an)
        key = first.notes[0].key
        c.put_games([b], "knightlyme")
        res = run_update(Config(**CFG), c, fetch=False, existing={key: first.notes[0].pgn}, analyser_factory=an)
        (changed,) = res.notes
        self.assertEqual((changed.key, res.new_cards), (key, 0))
        self.assertIn("<b>Similar positions</b>", changed.notes)
        self.assertIn("g6 would also be a good move if the white bishop on c4 was on b5.", changed.notes)
        self.assertIn("Nf6 would still be a bad move if the white bishop on c4 was on b5.", changed.notes)
        # Remembered: the similar position is not looked at again, nor is anything rewritten.
        again = run_update(
            Config(**CFG), c, fetch=False, existing={key: first.notes[0].pgn}, force=True, analyser_factory=an
        )
        self.assertEqual(again.notes, [])

    def test_a_similar_position_in_the_same_run_rewrites_the_note_just_made(self):
        a = recent()
        b = recent(id="test0002", moves=list(B_MOVES), end_ts=int(__import__("time").time()) - 60)

        def an(*_):
            f = FakeAnalyser(table={**PUNISH_TABLE, **B_TABLE})
            f.scores[fen_key(board_at(B_MOVES, 6).fen())] = S(400, 800, 200, 0)
            return f

        res = run_update(Config(**CFG), seed(self, [a, b]), fetch=False, analyser_factory=an)
        self.assertEqual(
            [(n.key, n.new_cards) for n in res.notes],
            [(puzzles.puzzle_key(PUZZLE_FEN, 5), 2), (puzzles.puzzle_key(PUZZLE_FEN, 5), 0)],
        )
        self.assertIn("would also be a good move", res.notes[-1].notes)
        self.assertNotIn("would also be a good move", res.notes[0].notes)

    def test_a_note_deleted_from_the_collection_is_made_again(self):
        c = seed(self, [recent()])
        run_update(Config(**CFG), c, fetch=False, analyser_factory=FakeAnalyser)
        again = run_update(Config(**CFG), c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual(again.new_cards, 1)

    def test_game_nags_grade_every_move(self):
        from knightly.select import game_nags

        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x")
        g = scholar_game()
        from helpers import PUZZLE_EVAL, WHITE_MATES
        from knightly.cache import fen_key

        after = fen_key(board_at(SCHOLAR_MOVES, 6).fen())  # Nf6 allows mate: the search after the move shows it
        table = {PUZZLE_FEN: PUZZLE_EVAL, after: Eval(depth=20, lines=[Line(WHITE_MATES, ["h5f7"])])}
        nags = game_nags(g, FakeAnalyser(table=table), cfg)
        self.assertEqual(nags[5], 4)  # Nf6??; (the scripted one-line mate position also reads as an only move)
        # Black's only good move at the puzzle position: marked as a singular move.
        table = {
            PUZZLE_FEN: Eval(
                depth=20,
                lines=[
                    Line(S(-30, 50, 850, 100), ["g8f6", "h5f7"]),
                    Line(S(300, 490, 500, 10), ["g7g6", "h5f3"]),
                    Line(S(300, 490, 500, 10), ["d8e7", "b1c3"]),
                ],
            )
        }
        seen = []
        game_nags(g, FakeAnalyser(), cfg, progress=lambda i, n: seen.append((i, n)))
        self.assertEqual(seen, [(i, 7) for i in range(7)])  # one report before each position
        g2 = scholar_game(moves=SCHOLAR_MOVES[:5] + ["g8f6"])
        self.assertEqual(game_nags(g2, FakeAnalyser(table=table), cfg), {5: 8})

    def test_run_update_without_fetch(self):
        c = seed(self, [recent()])
        res = run_update(Config(**CFG), c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual((res.new_cards, len(res.notes)), (1, 1))

    def test_skips_when_nothing_changed(self):
        c = seed(self, [recent()])
        cfg = Config(**CFG)
        calls = []

        def factory(*a):
            calls.append(1)
            return FakeAnalyser(*a)

        run = lambda cfg=cfg, **kw: run_update(cfg, c, fetch=False, analyser_factory=factory, **kw)
        first = run()
        existing = {first.notes[0].key: first.notes[0].pgn}
        self.assertFalse(run().skipped)  # cards were made last time and may never have been written
        run(existing=existing)  # settles: the new card is on the collection now
        n = len(calls)
        again = run(existing=existing)
        self.assertTrue(again.skipped)
        self.assertEqual((len(calls), again.notes, again.analysed_positions), (n, [], 0))
        self.assertFalse(run(existing=existing, force=True).skipped)
        # A setting change or a new game changes the inputs.
        self.assertFalse(run(Config(**CFG, min_loss=20), existing=existing).skipped)
        c.put_games([recent(end_ts=int(__import__("time").time()) - 60, id="other")], "knightlyme")
        self.assertFalse(run(existing=existing).skipped)

    def test_on_note_gets_each_note_as_it_is_built(self):
        seen = []
        res = run_update(
            Config(**CFG), seed(self, [recent()]), fetch=False, analyser_factory=FakeAnalyser, on_note=seen.append
        )
        self.assertEqual((res.new_cards, seen), (1, res.notes))
        # A cancel after the first note keeps it in the result.
        ev = threading.Event()
        res = run_update(
            Config(**CFG),
            seed(self, [recent()]),
            fetch=False,
            analyser_factory=FakeAnalyser,
            on_note=lambda spec: ev.set(),
            cancel=ev,
        )
        self.assertEqual(len(res.notes), 1)

    def test_precalculate_only_analyses(self):
        c = seed(self, [recent()])
        pre = run_update(Config(**CFG), c, fetch=False, analyser_factory=FakeAnalyser, precalculate=True)
        self.assertEqual((pre.new_cards, pre.notes, pre.skipped), (0, [], False))
        self.assertGreater(pre.analysed_positions, 0)
        self.assertEqual(run_update(Config(**CFG), c, fetch=False, analyser_factory=FakeAnalyser).new_cards, 1)

    def test_unfinished_run_is_not_skipped(self):
        c = seed(self, [recent()])
        cfg = Config(**CFG, analysis_minutes=1e-9)
        run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser)  # budget ran out at once
        self.assertFalse(run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser).skipped)

    def test_progress_events(self):
        events = []
        run_update(
            Config(**CFG, max_new_cards=3),
            seed(self, [recent()]),
            fetch=False,
            analyser_factory=FakeAnalyser,
            progress=lambda *e: events.append(e),
        )
        pos = [(d, t) for s, d, t, _ in events if s == "positions"]
        self.assertTrue(pos)
        self.assertTrue(all(0 <= d <= t for d, t in pos))
        self.assertTrue(all(t == 3 for s, _, t, _ in events if s == "analyse" and t))

    def test_newest_game_gets_a_shared_mistake(self):
        import time as _t

        now = int(_t.time())
        old_li = scholar_game(id="old", end_ts=now - 7200)
        new_cc = scholar_game(source="chesscom", id="new", end_ts=now - 60)
        c = tmpcache(self)
        c.put_games([old_li], "knightlyme")
        c.put_games([new_cc], "knightlyme")
        res = run_update(Config(**CFG, chesscom_user="KnightlyMe"), c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual(len(res.notes), 1)
        self.assertIn(new_cc.url, res.notes[0].pgn)  # made from the newest game that played it

    def test_cancel(self):
        ev = threading.Event()
        ev.set()

        class Cancelling(FakeAnalyser):
            def score(self, board, depth):
                raise http.Cancelled()

        self.assertTrue(
            run_update(
                Config(**CFG), seed(self, [recent()]), fetch=False, cancel=ev, analyser_factory=Cancelling
            ).cancelled
        )


class PipelineTest(unittest.TestCase):
    """The ply-by-ply mining (pipeline.py)."""

    def test_depth_steps(self):
        self.assertEqual(pipeline.depth_steps(13), [5, 9, 13])
        self.assertEqual(pipeline.depth_steps(20), [8, 14, 20])
        self.assertEqual(pipeline.depth_steps(3), [3])
        self.assertEqual(pipeline.scan_steps(13), [5, 9])
        self.assertEqual(pipeline.scan_steps(3), [3])

    def mine(self, games, script, cfg=None, **kw):
        cfg = cfg or Config(sf_depth=13, max_new_cards=9)
        an = ScriptedAnalyser(games, script)
        m = pipeline.Miner(
            sorted(games, key=lambda g: -g.end_ts), an, cfg, puzzles.Store(tmpcache(self)), want=cfg.max_new_cards, **kw
        )
        m.run()
        return m, an

    def test_the_biggest_losses_are_promoted_first_and_small_ones_dropped(self):
        g1 = game_from(SICILIAN, white=False, id="g1", end_ts=300)
        g2 = game_from(["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6"], white=False, id="g2", end_ts=200)
        g3 = game_from(["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"], white=False, id="g3", end_ts=100)
        m, an = self.mine([g1, g2, g3], {"g1": {5: 30}, "g2": {5: 15}, "g3": {5: 0.2}})
        child = lambda g: fen_key(board_at(g.moves, 6).fen())
        at = lambda k, d: [i for i, s in enumerate(an.searched) if s == (k, d)]
        self.assertTrue(all(at(child(g), 5) for g in (g1, g2, g3)))  # every move of every position, first
        self.assertTrue(at(child(g1), 9) and at(child(g2), 9))
        self.assertLess(at(child(g1), 9)[0], at(child(g2), 9)[0])  # the bigger loss first
        self.assertEqual(at(child(g3), 9), [])  # 0.2 % is nothing: dropped at the first step
        self.assertEqual(
            [n.key for n in m.notes][:2], [puzzles.puzzle_key(board_at(g.moves, 5).fen(), 5) for g in (g1, g2)]
        )
        self.assertEqual(len(m.notes), 2)

    def test_time_budget(self):
        t = [0.0]

        class Slow(ScriptedAnalyser):
            def score(self, board, depth):
                t[0] += 10  # seconds per position
                return super().score(board, depth)

        sic = game_from(SICILIAN, white=False, id="s1", end_ts=200)
        cfg = Config(sf_depth=13, analysis_minutes=1, max_new_cards=5)
        m = pipeline.Miner(
            [sic], Slow([sic], {"s1": {5: 30}}), cfg, puzzles.Store(tmpcache(self)), want=5, clock=lambda: t[0]
        )
        m.run()
        self.assertEqual((m.analysed, m.notes, m.finished), (6, [], False))


def _ev(*lines, depth=20):
    """An Eval from (uci, score) pairs, best first."""
    return Eval(depth, [Line(sc, [mv]) for mv, sc in lines])


def _mate(n):
    """White's view: White mates in n (n > 0), or is mated in -n."""
    return Score(mate=n, wdl=(1000, 0, 0) if n > 0 else (0, 0, 1000))


class FollowTest(unittest.TestCase):
    """Solution trees (follow.py): White solves from the start position."""

    def card(self, alternatives, mate=False):
        from knightly.model import CardSpec, Choice

        return CardSpec(
            fen_key=fen_key(chess.STARTING_FEN),
            ply=0,
            played=Choice("a2a3", MISTAKE, S(0, 0, 1000, 0)),
            alternatives=[Choice(m, GOOD, S(0, 0, 1000, 0)) for m in alternatives],
            loss=10,
            mate=mate,
        )

    def tree(self, table, alternatives=("e2e4",), mate=False, solver=chess.WHITE, board=None, **kw):
        from knightly import follow

        card = self.card(alternatives, mate)
        an = FakeAnalyser(table=table)
        follow.follow_puzzle(
            board or chess.Board(), card, an, Config(), solver=solver, seen=kw.pop("seen", set()), quick_depth=5, **kw
        )
        return card, an

    @staticmethod
    def key(*moves):
        return fen_key(board_at(list(moves), len(moves)).fen())

    @staticmethod
    def moves(ch):
        return [(c.move, c.nag) for c in ch.children]

    def test_the_defender_branches_on_good_replies_and_the_solver_on_a_big_gap(self):
        e4, e4e5, e4c5 = self.key("e2e4"), self.key("e2e4", "e7e5"), self.key("e2e4", "c7c5")
        table = {
            e4: _ev(
                ("e7e5", score_for(48)), ("c7c5", score_for(48.5)), ("d7d6", score_for(51))
            ),  # Black's view: 52, 51.5, 49
            e4e5: _ev(("g1f3", score_for(52)), ("b1c3", score_for(51.5)), ("f1c4", score_for(46))),  # gap 6: follow
            e4c5: _ev(("g1f3", score_for(52)), ("d2d4", score_for(51.6)), ("c2c3", score_for(48.5))),  # gap 3.5: stop
        }
        card, _ = self.tree(table)
        e4node = card.alternatives[0]
        self.assertEqual([c.move for c in e4node.children], ["e7e5", "c7c5"])  # d6 is 3 % worse: not a good reply
        e5, c5 = e4node.children
        self.assertEqual([c.move for c in e5.children], ["g1f3", "b1c3", "f1c4"])  # all moves of the solver, graded
        self.assertEqual([c.nag for c in e5.children], [GOOD, GOOD, DUBIOUS])
        self.assertEqual(c5.children, [])  # the gap to the best other move is under 5 %: the line ends

    def test_decided_games_end_with_a_comment(self):
        e4 = self.key("e2e4")
        table = {e4: _ev(("e7e5", score_for(30)), ("c7c5", score_for(35)))}  # even Black's worst move wins for Black
        card, _ = self.tree(table)
        node = card.alternatives[0]
        self.assertEqual((node.children, node.comment), ([], "The game is decided: Black is winning."))

    def test_a_stockfish_mate_ends_a_line_that_is_not_a_mate_puzzle(self):
        table = {self.key("e2e4"): _ev(("e7e5", _mate(3)), ("c7c5", _mate(3)))}
        card, _ = self.tree(table)
        self.assertEqual(card.alternatives[0].comment, "Mate in 3 according to Stockfish.")
        self.assertEqual(card.alternatives[0].children, [])

    def test_a_mate_puzzle_keeps_the_shortest_mate_and_the_longest_resistance(self):
        e4, e4e5, e4c5 = self.key("e2e4"), self.key("e2e4", "e7e5"), self.key("e2e4", "c7c5")
        table = {
            e4: _ev(("e7e5", _mate(3)), ("c7c5", _mate(3)), ("d7d6", _mate(2))),  # Black is mated in 3, 3 or 2
            e4e5: _ev(("g1f3", _mate(2)), ("b1c3", _mate(2)), ("f1c4", _mate(4))),  # a slower mate exists too: stop
            e4c5: _ev(("g1f3", _mate(2)), ("f1c4", score_for(70))),  # the next best is no mate: follow
        }
        card, _ = self.tree(table, mate=True)
        e5, c5 = card.alternatives[0].children
        self.assertEqual([c.move for c in card.alternatives[0].children], ["e7e5", "c7c5"])  # not d6 (mate in 2)
        self.assertEqual(e5.children, [])
        self.assertEqual([c.move for c in c5.children], ["g1f3", "f1c4"])

    def test_the_solver_being_mated_ends_the_line(self):
        e4 = self.key("e2e4")
        table = {
            e4: _ev(("e7e5", score_for(50)), ("c7c5", score_for(55))),
            self.key("e2e4", "e7e5"): _ev(("g1f3", _mate(-2)), ("b1c3", _mate(-2))),
        }  # White is mated either way
        card, _ = self.tree(table, mate=True)
        self.assertEqual(card.alternatives[0].children[0].children, [])

    def test_a_transposition_is_written_once(self):
        a = self.key("e2e4", "e7e5", "d2d4")
        table = {
            self.key("e2e4"): _ev(("e7e5", score_for(48)), ("d7d6", score_for(60))),
            self.key("e2e4", "e7e5"): _ev(("d2d4", score_for(53)), ("g1f3", score_for(46))),  # gap 7
            self.key("d2d4"): _ev(("e7e5", score_for(48)), ("d7d6", score_for(60))),
            self.key("d2d4", "e7e5"): _ev(("e2e4", score_for(53)), ("g1f3", score_for(46))),
            a: _ev(("e5d4", score_for(48)), ("d7d6", score_for(60))),
        }
        card, _ = self.tree(table, alternatives=("e2e4", "d2d4"), seen={fen_key(chess.STARTING_FEN)})
        first = card.alternatives[0].children[0].children[0]  # e4 e5 d4: written in full
        second = card.alternatives[1].children[0].children[0]  # d4 e5 e4: the same position
        self.assertEqual((first.move, second.move), ("d2d4", "e2e4"))
        self.assertTrue(first.children)
        self.assertEqual((second.children, second.comment), ([], "Transposes to a line above."))

    def test_draw_rules(self):
        from knightly.follow import draw_reason

        b = chess.Board()
        for m in ["g1f3", "g8f6", "f3g1", "f6g8"] * 2:
            b.push_uci(m)
        self.assertEqual(draw_reason(b), "Draw by threefold repetition.")
        self.assertEqual(draw_reason(chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 100 80")), "Draw by the fifty-move rule.")
        self.assertEqual(draw_reason(chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")), "Draw: not enough material.")
        self.assertEqual(draw_reason(chess.Board()), "")

    def test_a_drawn_line_is_not_followed(self):
        b = chess.Board()
        for m in ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"]:
            b.push_uci(m)
        card, _ = self.tree({}, alternatives=("f6g8",), solver=chess.BLACK, board=b)
        self.assertEqual(card.alternatives[0].comment, "Draw by threefold repetition.")

    def test_decided_positions_make_no_puzzle(self):
        from knightly.select import decided

        b = board_at(SCHOLAR_MOVES, 5)
        lines = lambda *pcts: {m: Line(score_for(100 - p), [m]) for m, p in zip(["g7g6", "d8e7", "d8f6"], pcts)}
        self.assertTrue(decided(b, lines(60, 55, 52)))  # Black wins whatever it plays
        self.assertFalse(decided(b, lines(45, 40, 30)))  # loses whatever it plays
        self.assertIsNone(decided(b, lines(60, 55, 40)))
        self.assertIsNone(decided(b, {"g8f6": Line(WHITE_MATES, ["g8f6"]), "g7g6": Line(score_for(40), ["g7g6"])}))
        table = {PUZZLE_FEN: Eval(20, [Line(S(-30, 50, 850, 100), ["g7g6"]), Line(S(-25, 70, 850, 80), ["g8f6"])])}
        self.assertIsNone(build_card(b, 5, "g8f6", FakeAnalyser(table=table), Config()))  # every move holds above 50 %

    def test_a_decided_punishment_makes_no_second_card(self):
        after = board_at(SCHOLAR_MOVES, 6)
        table = {fen_key(after.fen()): _ev(("h5h4", score_for(80)), ("h5g4", score_for(75)))}  # White wins whatever
        self.assertIsNone(build_punish(after, 6, "Nf6", FakeAnalyser(table=table), Config()))


class ProgressTest(unittest.TestCase):
    def test_plan_and_advance_count_like_a_queue(self):
        t = Tracker()
        t.stage("analyse", "Ply 0", total=3, unit="moves", step="depth 5 of 13")
        t.plan(2)
        threads = [threading.Thread(target=lambda: [t.advance() for _ in range(50)]) for _ in range(4)]
        [th.start() for th in threads]
        [th.join() for th in threads]
        s = t.snapshot()
        self.assertEqual((s.done, s.total, s.unit, s.step), (200, 5, "moves", "depth 5 of 13"))

    def test_stage_changes_are_forced_and_advances_throttled(self):
        now = [0.0]
        t = Tracker(clock=lambda: now[0], min_interval=1.0)
        seen = []
        t.listen(lambda s: seen.append((s.stage, s.done)))
        t.stage("analyse", "a", total=10)
        for _ in range(5):
            t.advance()  # within the interval: not delivered
        self.assertEqual(seen, [("analyse", 0)])
        now[0] = 2.0
        t.advance()
        t.stage("analyse", "b", total=4)  # forced even right after
        self.assertEqual(seen, [("analyse", 0), ("analyse", 6), ("analyse", 0)])

    def test_rate_and_eta(self):
        now = [0.0]
        t = Tracker(clock=lambda: now[0])
        t.stage("analyse", "x", total=100)
        for i in range(1, 11):
            now[0] = i * 0.5
            t.advance()
        s = t.snapshot()
        self.assertAlmostEqual(s.rate, 2.0, places=1)
        self.assertAlmostEqual(s.eta, 45.0, places=0)
        d = progress.describe(s)
        self.assertIn("10 of 100", d["stage"])
        self.assertIn("2.0/s", d["stage"])
        self.assertAlmostEqual(d["stage_ratio"], 0.1)

    def test_sub_stage_restores_the_outer_one(self):
        t = Tracker()
        t.stage("analyse", "outer", total=10, unit="moves")
        t.advance(4)
        with t.sub("cards", "grading", total=7, unit="positions"):
            t.advance()
            self.assertEqual((t.snapshot().stage, t.snapshot().done, t.snapshot().total), ("cards", 1, 7))
        s = t.snapshot()
        self.assertEqual((s.stage, s.message, s.done, s.total), ("analyse", "outer", 4, 10))

    def test_engine_rows(self):
        t = Tracker()
        t.engine("stockfish", "Stockfish 17", True)
        t.engine("lc0", "Lc0 0.31", True)
        t.search_start("stockfish", chess.STARTING_FEN, 0, 20, 13)
        t.search_progress("stockfish", depth=9, nodes=10**6, nps=1_200_000, ms=800)
        d = progress.describe(t.snapshot())["engines"]
        self.assertEqual(d["stockfish"]["detail"], "ply 0 · depth 9 of 13 · 20 lines · 1200 knps")
        self.assertAlmostEqual(d["stockfish"]["ratio"], 9 / 13)
        self.assertEqual(d["lc0"]["detail"], "idle")
        t.search_end("stockfish", ms=900)
        t.search_end("stockfish", cached=True)
        self.assertEqual(
            progress.describe(t.snapshot())["engines"]["stockfish"]["counts"], "1 searched · 1 from the cache"
        )
        t.search_start("lc0", chess.STARTING_FEN, 3, 1, 900)
        t.search_progress("lc0", ms=450)
        self.assertEqual(progress.describe(t.snapshot())["engines"]["lc0"]["detail"], "ply 3 · 0.5 of 0.9 s")
        # A disabled engine is not described.
        self.assertNotIn("lc0", progress.describe(Tracker().snapshot())["engines"])

    def test_legacy_listener(self):
        calls = []
        t = Tracker()
        t.listen(progress.legacy_listener(lambda *a: calls.append(a)))
        t.found(0, 3)
        t.stage("analyse", "Ply 0 (0 of 3 puzzles found)", total=4, unit="moves", step="depth 5 of 13")
        self.assertEqual(
            calls, [("analyse", 0, 3, "Ply 0 (0 of 3 puzzles found)"), ("positions", 0, 4, "depth 5 of 13")]
        )

    def test_engine_streams_progress_and_caches_like_analyse(self):
        from knightly.engine.analyse import Engine

        t = Tracker()
        t.engine("stockfish", "SF", True)
        seen = []

        class Proc:
            def analysis(self, board, limit, multipv, root_moves=None):
                return FakeAnalysis(
                    [sf_info(5), sf_info(9), sf_info(13)],
                    on_info=lambda i: seen.append(t.snapshot().engines["stockfish"].depth),
                )

        c = tmpcache(self)
        eng = Engine("stockfish", "x", Config(sf_depth=13), c, tracker=t)
        eng._proc = Proc()
        b = chess.Board()
        ev = eng.evaluate(b, 1)
        self.assertEqual(seen, [0, 5, 9])  # the depth shown while each info arrives (the last one is current after)
        self.assertEqual(t.snapshot().engines["stockfish"].depth, 13)
        self.assertEqual(ev.lines[0].moves, ["e2e4"])
        e = t.snapshot().engines["stockfish"]
        self.assertEqual((e.searches, e.cached, e.busy), (1, 0, False))
        eng.evaluate(b, 1)  # from the cache
        e = t.snapshot().engines["stockfish"]
        self.assertEqual((e.searches, e.cached), (1, 1))

    def test_cancel_stops_a_search_in_progress(self):
        from knightly.engine.analyse import Engine

        cancel = threading.Event()
        made = []

        class Proc:
            def analysis(self, board, limit, multipv, root_moves=None):
                a = FakeAnalysis(
                    [sf_info(5), sf_info(9), sf_info(13)], on_info=lambda i: cancel.set() if i["depth"] == 9 else None
                )
                made.append(a)
                return a

        eng = Engine("stockfish", "x", Config(sf_depth=13), tmpcache(self), cancel=cancel)
        eng._proc = Proc()
        with self.assertRaises(http.Cancelled):
            eng.evaluate(chess.Board(), 1)
        self.assertTrue(made[0].stopped)

    def test_a_run_reports_consistent_stages(self):
        c = seed(self, [recent()])
        t = Tracker()
        stages = []
        t.listen(lambda s: stages.append((s.stage, s.done, s.total)))
        res = run_update(
            Config(**CFG), c, fetch=False, analyser_factory=lambda *a: FakeAnalyser(table=PUNISH_TABLE), tracker=t
        )
        self.assertEqual(res.new_cards, 2)
        self.assertTrue(stages)
        self.assertTrue(all(d <= tot for _, d, tot in stages if tot))
        s = t.snapshot()
        self.assertEqual((s.found, s.want), (2, 10))  # cards made
        self.assertGreater(s.max_ply, 0)


class SubNoteTest(unittest.TestCase):
    """A decision position inside a mistake's tree is a note of its own, made before the mistake's (leaf to root)."""

    def pos(self, *moves):
        b = board_at(SCHOLAR_MOVES, 5)
        for m in moves:
            b.push_uci(m)
        return b

    def table(self):
        e = fen_key(self.pos("d8e7").fen())  # after 3... Qe7: White to move (the opponent)
        x1 = fen_key(self.pos("d8e7", "b1c3").fen())  # Black's decision: g6 and h6 are good, b6 loses 8 %
        x2 = fen_key(self.pos("d8e7", "d2d3").fen())  # a small gap: the line ends there
        q = fen_key(self.pos("d8e7", "b1c3", "b7b6").fen())  # after the mistake b6: White punishes
        qxf7 = fen_key(self.pos("d8e7", "b1c3", "b7b6", "h5f7").fen())
        return {
            **PUNISH_TABLE,
            e: _ev(("b1c3", score_for(51)), ("d2d3", score_for(50.5)), ("a2a3", score_for(40))),
            x1: _ev(("g7g6", score_for(48)), ("h7h6", score_for(48.5)), ("b7b6", score_for(56))),
            x2: _ev(("g7g6", score_for(48)), ("b7b6", score_for(50))),
            q: _ev(("h5f7", score_for(70)), ("g1f3", score_for(69.5)), ("a2a3", score_for(40))),
            qxf7: _ev(("e7f7", score_for(50)), ("e8d8", score_for(70))),
        }

    def go(self, games=None, existing=None, **kw):
        c = seed(self, games or [recent()])
        table = self.table()
        return c, run_update(
            Config(**CFG, **kw),
            c,
            fetch=False,
            existing=existing or {},
            analyser_factory=lambda *a: FakeAnalyser(table=table),
        )

    def test_the_deepest_position_is_made_first_and_the_mistake_last(self):
        _, res = self.go()
        keys = [n.key for n in res.notes]
        x1 = self.pos("d8e7", "b1c3")
        self.assertEqual(keys, [puzzles.puzzle_key(fen_key(x1.fen()), x1.ply()), puzzles.puzzle_key(PUZZLE_FEN, 5)])
        sub = res.notes[0]
        dated = re.sub(r'\[Date "[^"]*"\]', '[Date "?"]', sub.pgn)  # the game is recent: its date moves
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN_SUB.write_text(dated + "\n")
        self.assertEqual(dated + "\n", GOLDEN_SUB.read_text())  # the card's tests play it too
        self.assertEqual((sub.new_cards, sub.cards), (2, "{{c1::1::1}} {{c2::2::2}}"))
        # It starts from the position before the opponent's last move, which is its first move.
        self.assertIn('[FEN "', sub.pgn)
        games = chess.pgn.read_game(io.StringIO(sub.pgn))
        self.assertEqual(games.headers["SetUp"], "1")
        self.assertEqual(games.next().san(), "Nc3")
        mistake = games.next().next()
        self.assertEqual((mistake.san(), mistake.comment.count("[%kr 1]")), ("b6", 1))
        self.assertEqual(notes.card_ids(sub.pgn), {fen_key(x1.fen()): 1, self.q(): 2})
        self.assertIn("b6 loses 8% win rate. g6 was best.", sub.pgn)
        self.assertEqual(res.new_cards, 4)

    def q(self):
        return fen_key(self.pos("d8e7", "b1c3", "b7b6").fen())

    def test_the_mistake_is_the_non_good_move_our_games_played(self):
        # Black has two moves that are not good at the decision position: b6 (the best of them, 8 %) and a6 (9 %).
        table = self.table()
        x1 = fen_key(self.pos("d8e7", "b1c3").fen())
        table[x1] = _ev(
            ("g7g6", score_for(48)), ("h7h6", score_for(48.5)), ("b7b6", score_for(56)), ("a7a6", score_for(57))
        )
        g2 = recent(
            id="g2", end_ts=int(__import__("time").time()) - 60, moves=SCHOLAR_MOVES[:5] + ["d8e7", "b1c3", "a7a6"]
        )
        c = seed(self, [recent(), g2])
        res = run_update(
            Config(**CFG, max_new_cards=40), c, fetch=False, analyser_factory=lambda *a: FakeAnalyser(table=table)
        )
        sub = chess.pgn.read_game(io.StringIO(res.notes[0].pgn))
        self.assertEqual(sub.next().next().san(), "a6")  # played in a game, though b6 is the better mistake
        _, plain = self.go()
        self.assertEqual(chess.pgn.read_game(io.StringIO(plain.notes[0].pgn)).next().next().san(), "b6")

    def test_a_note_is_one_move_and_the_reply(self):
        _, res = self.go()
        sub = chess.pgn.read_game(io.StringIO(res.notes[0].pgn))
        mistake = sub.next().next()
        punish = mistake.variations[0]
        self.assertEqual(punish.san(), "Qxf7+")
        self.assertIn("[%kr 2]", punish.comment)
        self.assertEqual([v.san() for v in punish.variations], ["Qxf7"])  # the response, and nothing after it
        self.assertEqual(punish.variations[0].variations, [])
        alts = {v.san() for v in sub.next().variations}
        self.assertTrue({"g6", "h6"} <= alts)

    def test_no_position_is_made_twice(self):
        _, first = self.go()
        existing = {n.key: n.pgn for n in first.notes}
        _, again = self.go(existing=existing)
        self.assertEqual(again.notes, [])
        # A game that makes the mistake at the position of a note is not a new puzzle.
        g2 = recent(
            id="g2", end_ts=int(__import__("time").time()) - 60, moves=SCHOLAR_MOVES[:5] + ["d8e7", "b1c3", "b7b6"]
        )
        table = self.table()

        def an(*_):
            f = FakeAnalyser(table=table)
            f.scores[self.q()] = score_for(56)  # the bad move b6 scores as a mistake
            return f

        c = seed(self, [recent(), g2])
        res = run_update(Config(**CFG, max_new_cards=40), c, fetch=False, existing=existing, analyser_factory=an)
        self.assertEqual(res.notes, [])

    def test_the_card_limit_counts_cards_and_the_run_resumes(self):
        c, res = self.go(max_new_cards=1)
        self.assertEqual(len(res.notes), 1)  # the deepest note (2 cards) reached the limit: the mistake waits
        existing = {n.key: n.pgn for n in res.notes}
        table = self.table()
        res2 = run_update(
            Config(**CFG, max_new_cards=1),
            c,
            fetch=False,
            existing=existing,
            analyser_factory=lambda *a: FakeAnalyser(table=table),
        )
        self.assertEqual([n.key for n in res2.notes], [puzzles.puzzle_key(PUZZLE_FEN, 5)])

    def test_no_second_card_when_the_punish_position_is_a_card_already(self):
        _, first = self.go()
        # Keep only the sub-note: the root's second position (after Nf6) is free, but the sub-note's own
        # punish position is claimed by it, so a repeat makes nothing of it.
        existing = {first.notes[0].key: first.notes[0].pgn}
        _, again = self.go(existing=existing)
        self.assertEqual([n.new_cards for n in again.notes], [2])  # only the mistake's own note


class StatsTest(unittest.TestCase):
    def test_collect_counts_moves_by_side(self):
        a = game_from(SICILIAN, white=False, id="s1", end_ts=200)
        b = game_from(FRENCH, white=True, id="f1", end_ts=100)  # the user is White here
        by_ply = stats.collect([a, b])
        start = by_ply[0][fen_key(chess.Board().fen())]
        self.assertEqual({u: (st.mine, st.opp) for u, st in start.moves.items()}, {"e2e4": (1, 1)})
        after_e4 = by_ply[1][fen_key(board_at(SICILIAN, 1).fen())]
        self.assertEqual({u: (st.mine, st.opp) for u, st in after_e4.moves.items()}, {"c7c5": (1, 0), "e7e6": (0, 1)})
        self.assertEqual(after_e4.moves["c7c5"].mine_game, a)  # the newest game that played it

    def test_positions_are_matched_whatever_the_ply_and_count_each_game_once(self):
        a = game_from(SICILIAN, white=False, id="s1", end_ts=200, result="1-0")
        b = game_from(["g1f3", "g8f6", "e2e4", "e7e5"], white=True, id="x", result="1/2-1/2")
        c = game_from(["e2e4", "e7e5", "g1f3", "g8f6"], white=True, id="y", result="0-1")  # 1.e4 e5 2.Nf3 Nf6
        pos = stats.collect_positions([a, b, c])
        start = pos[fen_key(chess.Board().fen())]
        self.assertEqual((start.reached, {u: m.games for u, m in start.moves.items()}), (3, {"e2e4": 2, "g1f3": 1}))
        self.assertEqual(
            {u: (m.white, m.draw, m.black) for u, m in start.moves.items()}, {"e2e4": (1, 0, 1), "g1f3": (0, 1, 0)}
        )
        # After 1.e4 e5 2.Nf3 Nf6 and 1.Nf3 Nf6 2.e4 e5: the same position at the same ply, two games.
        meet = pos[fen_key(board_at(["e2e4", "e7e5", "g1f3", "g8f6"], 4).fen())]
        self.assertEqual(meet.reached, 2)
        t = start.totals()
        self.assertEqual((t.games, t.white, t.draw, t.black), (3, 1, 1, 1))

    def test_a_game_that_repeats_a_position_counts_once(self):
        g = game_from(["g1f3", "g8f6", "f3g1", "f6g8", "g1f3"], white=True, id="r", result="1-0")
        start = stats.collect_positions([g])[fen_key(chess.Board().fen())]
        self.assertEqual(start.reached, 1)
        self.assertEqual(start.moves["g1f3"].games, 1)

    def test_unfinished_games_count_as_played_not_as_a_result(self):
        a = game_from(FRENCH, white=True, id="a", result="*")
        b = game_from(FRENCH, white=True, id="b", result="1-0")
        pos = stats.collect_positions([a, b])[fen_key(chess.Board().fen())]
        self.assertEqual((pos.moves["e2e4"].games, pos.moves["e2e4"].decided), (2, 1))


class StatsViewTest(unittest.TestCase):
    def pos(self, games):
        return stats.collect_positions(games)[fen_key(chess.Board().fen())]

    def test_percentages_always_add_up_to_100(self):
        for parts in ([1, 1, 1], [5, 3, 4], [1, 0, 0], [7, 2, 2], [0, 0, 0]):
            got = statsview.percents(parts)
            self.assertEqual(sum(got), 100 if sum(parts) else 0, parts)
        self.assertEqual(statsview.percents([1, 1, 1]), [34, 33, 33])

    def test_a_position_needs_two_distinct_games(self):
        one = self.pos([game_from(FRENCH, white=True, id="a")])
        self.assertEqual(statsview.section(1, one), "")
        self.assertEqual(statsview.section(1, None), "")
        two = self.pos([game_from(FRENCH, white=True, id="a"), game_from(FRENCH, white=True, id="b")])
        self.assertIn('<div class="kr-stats" data-card="1">', statsview.section(1, two))

    def test_rows_bars_and_badges(self):
        games = (
            [game_from(["e2e4"], white=True, id=f"e{i}", result="1-0") for i in range(3)]
            + [game_from(["e2e4"], white=True, id="ed", result="1/2-1/2")]
            + [game_from(["d2d4"], white=True, id="d1", result="0-1"), game_from(["a2a3"], white=True, id="a1")]
        )
        html_ = statsview.section(1, self.pos(games), nags={"e2e4": GOOD, "a2a3": MISTAKE}, mistake="a2a3")
        self.assertIn("6 games reached this position · White 4 (67%) · draws 1 (17%) · Black 1 (16%)", html_)
        self.assertIn('<span class="kr-stat-move">e4<b class="kr-stat-nag">!</b></span>', html_)
        self.assertIn("4 games · 67%", html_)
        self.assertLess(html_.index("e4"), html_.index("d4"))  # most played first
        self.assertIn("kr-stat-row kr-good", html_)
        self.assertIn("kr-stat-row kr-bad", html_)  # the mistake
        self.assertIn('title="White 3 · draws 1 · Black 0"', html_)  # e4: 4 games, 1 draw, no Black win

    def test_only_six_rows_and_the_rest_in_one(self):
        first = ["a2a3", "b2b3", "c2c3", "d2d3", "e2e3", "f2f3", "g2g3", "h2h3"]
        games = [game_from([m], white=True, id=f"g{i}", result="1-0") for i, m in enumerate(first)]
        html_ = statsview.section(1, self.pos(games))
        self.assertEqual(html_.count('<div class="kr-stat-row'), 7)
        self.assertIn("other 2 moves", html_)

    def test_small_segments_have_no_label_and_a_section_names_its_card(self):
        games = [game_from(["e2e4"], white=True, id=f"w{i}", result="1-0") for i in range(30)]
        games.append(game_from(["e2e4"], white=True, id="b", result="0-1"))
        h = statsview.section(2, self.pos(games), heading="After the mistake: ")
        self.assertIn('data-card="2"', h)
        self.assertIn("After the mistake: 31 games reached this position", h)
        self.assertIn('<i class="b" style="width:3%"></i>', h)  # no label under 8 %


class SimilarTest(unittest.TestCase):
    def boards(self, a, b):
        return board_at(a, len(a)), board_at(b, len(b))

    def test_similar_means_same_side_and_few_squares(self):
        scholar, bishop_b5 = self.boards(SCHOLAR_MOVES[:5], B_MOVES[:5])
        self.assertTrue(similar.is_similar(scholar, bishop_b5))
        self.assertFalse(similar.is_similar(scholar, board_at(SCHOLAR_MOVES, 4)))  # the other side to move
        self.assertFalse(similar.is_similar(scholar, chess.Board()))
        self.assertTrue(similar.is_similar(scholar, scholar))  # the same position at another ply

    def test_describe(self):
        scholar, bishop_b5 = self.boards(SCHOLAR_MOVES[:5], B_MOVES[:5])
        self.assertEqual(similar.describe(scholar, bishop_b5), "the white bishop on c4 was on b5")
        a = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        b = chess.Board("4k3/8/8/8/8/8/PP6/4K3 w - - 0 1")
        self.assertEqual(similar.describe(a, b), "there was a white pawn on b2")
        c = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        self.assertEqual(similar.describe(a, c), "the white pawn on a2 was gone")
        self.assertIn("same position", similar.describe(a, a))

    def test_comments(self):
        w = "the pawn was on h4"
        self.assertEqual(similar.comment("Ng4", True, True, w), "Ng4 would also be a good move if the pawn was on h4.")
        self.assertEqual(similar.comment("Ng4", True, False, w), "Ng4 would not be a good move if the pawn was on h4.")
        self.assertEqual(
            similar.comment("Be3", False, True, w), "Be3 is not a good move, but it would be if the pawn was on h4."
        )
        self.assertEqual(
            similar.comment("Be3", False, False, w), "Be3 would still be a bad move if the pawn was on h4."
        )


class AnalyserTest(unittest.TestCase):
    """Combining Stockfish and Lc0, with the engine processes replaced by scripted answers."""

    def analyser(self, sf: Eval, lc: Eval, use_lc0=True):
        from knightly.engine import analyse

        cfg = Config(stockfish_path="sf", use_lc0=use_lc0, lc0_path="lc0")
        an = analyse.Analyser(cfg, tmpcache(self))
        self.addCleanup(an.close)
        self.lc0_ms = []
        an.sf.evaluate = lambda board, n, moves=None, **kw: sf
        if an.lc0:
            an.lc0.evaluate = lambda board, n, moves=None, ms=None, **kw: self.lc0_ms.append(ms) or lc
        return an

    SF = Eval(13, [Line(S(40, 300, 650, 50), ["e2e4"])], "stockfish", ms=840)
    LC = Eval(5, [Line(S(20, 200, 650, 150), ["d2d4"])], "lc0")
    SF_MATE = Eval(13, [Line(Score(mate=3, wdl=(1000, 0, 0)), ["e2e4"])], "stockfish")
    LC_MATE = Eval(5, [Line(Score(mate=5, wdl=(990, 10, 0)), ["d2d4"])], "lc0")

    def test_lc0_answers_unless_mate(self):
        b = chess.Board()
        self.assertIs(self.analyser(self.SF, self.LC).evaluate(b), self.LC)
        self.assertIs(self.analyser(self.SF_MATE, self.LC).evaluate(b), self.SF_MATE)
        self.assertIs(self.analyser(self.SF, self.LC_MATE).evaluate(b), self.SF)  # Stockfish decides mates
        an = self.analyser(self.SF, self.LC)
        an.evaluate(b)
        an.evaluate(b)
        self.assertEqual(an.picked, {"stockfish": 0, "lc0": 2})
        self.assertEqual(self.lc0_ms, [840, 840])  # Lc0 searches as long as Stockfish took

    def test_sf_only_skips_lc0(self):
        an = self.analyser(self.SF, self.LC)
        self.assertIs(an.evaluate(chess.Board(), sf_only=True), self.SF)
        self.assertEqual(an.picked, {"stockfish": 1, "lc0": 0})

    def test_stockfish_alone(self):
        an = self.analyser(self.SF, self.LC, use_lc0=False)
        self.assertIsNone(an.lc0)
        self.assertIs(an.evaluate(chess.Board()), self.SF)

    def test_limits(self):
        from knightly.engine.analyse import Engine

        cfg = Config(sf_depth=22)
        sf, lc = Engine("stockfish", "x", cfg, None), Engine("lc0", "x", cfg, None)
        self.assertEqual((sf.limit()[0].depth, sf.limit()[1]), (22, 22))
        self.assertEqual(sf.limit(depth=3)[0].depth, 3)
        self.assertEqual((lc.limit(ms=750)[0].time, lc.limit(ms=750)[1]), (0.75, 750))
        with self.assertRaises(ValueError):
            lc.limit()

    def test_search_time_is_stored_and_old_results_rerun(self):
        from knightly.engine.analyse import Engine

        class Proc:
            calls = 0

            def analysis(self, board, limit, multipv, root_moves=None):
                Proc.calls += 1
                return FakeAnalysis([sf_info(limit.depth)])

        c = tmpcache(self)
        b = chess.Board()
        k = fen_key(b.fen())
        c.put_eval(k, "stockfish", 13, Eval(13, [Line(S(1, 1, 998, 1), ["d2d4"])], "stockfish"))  # no time
        eng = Engine("stockfish", "x", Config(sf_depth=13), c)
        eng._proc = Proc()
        self.assertEqual(eng.evaluate(b, 1).lines[0].moves, ["d2d4"])  # cached is fine untimed
        ev = eng.evaluate(b, 1, timed=True)  # but not when its time is needed
        self.assertEqual((Proc.calls, ev.lines[0].moves), (1, ["e2e4"]))
        self.assertGreater(ev.ms, 0)
        self.assertEqual(c.get_eval(k, "stockfish", min_effort=13).ms, ev.ms)  # stored with the result
        eng.evaluate(b, 1, timed=True)
        self.assertEqual(Proc.calls, 1)

    def test_old_engine_path_setting(self):
        self.assertEqual(Config.from_dict({"engine_path": "/usr/bin/stockfish"}).stockfish_path, "/usr/bin/stockfish")
        self.assertEqual(Config.from_dict({"engine_path": "a", "stockfish_path": "b"}).stockfish_path, "b")


class EngineTest(unittest.TestCase):
    def test_pick_asset(self):
        assets = [
            {"name": n}
            for n in (
                "stockfish-linux-x86-64-universal.tar.gz",
                "stockfish-linux-arm64-universal.tar.gz",
                "stockfish-macos-universal.tar.gz",
                "stockfish-windows-x86-64-universal.zip",
            )
        ]
        self.assertEqual(install.pick_asset(assets, "linux", "x86_64")["name"], assets[0]["name"])
        self.assertEqual(install.pick_asset(assets, "linux", "aarch64")["name"], assets[1]["name"])
        self.assertEqual(install.pick_asset(assets, "darwin", "arm64")["name"], assets[2]["name"])
        self.assertEqual(install.pick_asset(assets, "win32", "AMD64")["name"], assets[3]["name"])
        self.assertIsNone(install.pick_asset(assets, "linux", "riscv64"))

    def test_safe_extract_refuses_traversal(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d)
        evil = d / "evil.tar"
        payload = d / "p"
        payload.write_text("x")
        with tarfile.open(evil, "w") as t:
            t.add(payload, arcname="../escaped")
        with self.assertRaises(ValueError):
            install._safe_extract(evil, d / "out")

    @unittest.skipIf(sys.platform == "win32", "uses a shebang script")
    def test_probe_and_discover_fake_engine(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d)

        def fake(name: str, wdl: bool) -> Path:
            opt = "print('option name UCI_ShowWDL type check default false'); " if wdl else ""
            p = d / name
            p.write_text(
                f"#!{sys.executable}\nimport sys\nfor l in sys.stdin:\n"
                f"    if l.strip()=='uci': print('id name {name}'); {opt}print('uciok'); sys.stdout.flush()\n"
                "    if l.strip()=='quit': break\n"
            )
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
            return p

        good = fake("stockfish-wdl", True)
        old = fake("stockfish-old", False)
        other = fake("stockfish-komodo", True)
        other.write_text(other.read_text().replace("id name stockfish-komodo", "id name Komodo 14"))
        self.assertEqual(discover.probe(str(good)), "stockfish-wdl")
        self.assertIsNone(discover.probe(str(old)))  # no UCI_ShowWDL: not supported
        self.assertEqual(discover.probe_details(str(old)), ("stockfish-old", False))
        found = [(e.name, e.found_in) for e in discover.discover(engines_dir=str(d))]
        self.assertIn(("stockfish-wdl", "downloaded"), found)
        self.assertNotIn(("stockfish-old", "downloaded"), found)
        self.assertIsNone(discover.probe(str(other)))  # only Stockfish and Lc0 are supported
        self.assertEqual(discover.kind_of("Lc0 v0.32.1"), "lc0")
        self.assertEqual(discover.kind_of("Stockfish 19"), "stockfish")
        not_engine = d / "nope"
        not_engine.write_text("#!/bin/sh\necho hello\n")
        not_engine.chmod(0o755)
        self.assertIsNone(discover.probe_details(str(not_engine)))


if __name__ == "__main__":
    unittest.main()

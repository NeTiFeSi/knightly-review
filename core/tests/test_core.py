from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

import chess

from helpers import (FIXTURES, PUZZLE_FEN, SCHOLAR_MOVES, SCHOLAR_SCORES, FakeAnalyser, FakeResponse,
                     FakeSession, S, board_at, scholar_game)
from knightly import http, notes, sources
from knightly.cache import SqliteCache, fen_key
from knightly.config import Config
from knightly.engine import discover, install
from knightly.jobs import run_update
from knightly.model import Eval, Game, Line, NoWdl, Score
from knightly.select import (BLUNDER, BRILLIANT, DUBIOUS, GOOD, INTERESTING, MISTAKE, SINGULAR, Graded, build_card,
                             find_candidates, grade, line_material)
from knightly.sources import chesscom, lichess

GOLDEN = FIXTURES / "generated_note.pgn"


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


class ChesscomTest(unittest.TestCase):
    def test_parse_and_fetch_with_etag(self):
        month = json.loads((FIXTURES / "chesscom_2026_10.json").read_text())
        g = chesscom.parse_game(month["games"][0], "knightlyme")
        self.assertEqual((g.source, g.time_class, g.user_is_white), ("chesscom", "correspondence", False))
        self.assertIsNone(chesscom.parse_game(month["games"][1], "knightlyme"))  # chess960
        c = tmpcache(self)
        arch = {"archives": ["https://api.chess.com/pub/player/knightlyme/games/2026/09",
                             "https://api.chess.com/pub/player/knightlyme/games/2026/10"]}
        s = FakeSession([
            ("/games/archives", FakeResponse(200, arch)),
            ("/games/2026/10", [FakeResponse(200, month, {"ETag": 'W/"abc"'}), FakeResponse(304)]),
            ("/games/2026/09", FakeResponse(200, {"games": []})),
        ])
        now = 1793000000  # 2026-10-26: October is the current month
        self.assertEqual(chesscom.fetch(c, "KnightlyMe", session=s, history_days=60, now=now), 2)
        self.assertEqual(chesscom.fetch(c, "KnightlyMe", session=s, history_days=60, now=now), 0)
        oct_reqs = [kw for url, kw in s.requests if url.endswith("2026/10")]
        self.assertEqual(oct_reqs[1]["headers"]["If-None-Match"], 'W/"abc"')
        self.assertEqual(sum(url.endswith("2026/09") for url, _ in s.requests), 1)  # finished month: once

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
        self.assertEqual(self.g([("takes_knight", 0, 3), ("takes_queen", 50, 9)]),
                         {"takes_knight": SINGULAR, "takes_queen": BLUNDER})
        # Giving back less than the material you're up still counts against the current balance.
        self.assertEqual(self.g([("a", 0, 2), ("b", 30, 5)], current=5), {"a": BRILLIANT, "b": BLUNDER})

    def test_interesting_and_good(self):
        nags = self.g([("sac", 0, -3), ("safe", 1.5, 0), ("bad", 8, 0)])
        self.assertEqual(nags, {"sac": INTERESTING, "safe": GOOD, "bad": DUBIOUS})
        # Two good moves that sacrifice the same: neither has a good alternative keeping more material.
        self.assertEqual(self.g([("x", 0, -3), ("y", 1, -3), ("greedy", 25, 0)]),
                         {"x": GOOD, "y": GOOD, "greedy": BLUNDER})

    def test_bad_move_bands_and_threshold(self):
        nags = self.g([("best", 0, 0), ("a", 3, 0), ("b", 10, 0), ("c", 10.5, 0), ("d", 20, 0), ("e", 21, 0)])
        self.assertEqual([nags[k] for k in "abcde"], [DUBIOUS, DUBIOUS, MISTAKE, MISTAKE, BLUNDER])
        self.assertEqual(self.g([("best", 0, 0), ("a", 1.5, 0)])["a"], GOOD)
        self.assertEqual(self.g([("best", 0, 0), ("a", 1.5, 0)], good_move=1.0)["a"], DUBIOUS)

    def test_line_material(self):
        b = chess.Board()
        # Counted at 4 plies, but Nxf7 is a capture in progress: follow it through Kxf7.
        self.assertEqual(line_material(b, ["g1f3", "e7e5", "f3e5", "d7d6", "e5f7", "e8f7"], 4), -1)
        # The recapture finishes the exchange; Nc3 is quiet.
        self.assertEqual(line_material(b, ["e2e4", "d7d5", "e4d5", "d8d5", "b1c3", "d5a5"], 4), 0)
        # A short line is continued by the engine when given one.
        short = chess.Board("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1")
        table = {fen_key(chess.Board("4k3/8/8/3P4/8/8/8/4K3 b - - 0 1").fen()):
                 Eval(20, [Line(S(0, 0, 1000, 0), ["e8d7", "e1e2", "d7d6", "e2d3"])])}
        self.assertEqual(line_material(short, ["e4d5"], 4, FakeAnalyser(table=table)), 1)

    def test_find_candidates(self):
        g = scholar_game()
        cands = find_candidates([(g, SCHOLAR_SCORES)], self.cfg)
        self.assertEqual([(c.ply, c.played) for c in cands], [(5, "g8f6")])
        self.assertEqual(cands[0].fen_key, PUZZLE_FEN)
        self.assertAlmostEqual(cands[0].loss, 52.5)
        self.assertEqual(cands[0].phase, "opening")
        self.assertEqual(find_candidates([(g, SCHOLAR_SCORES)], self.cfg, exclude={PUZZLE_FEN}), [])

    def test_opponent_mistakes_are_optional(self):
        g = scholar_game()
        scores = list(SCHOLAR_SCORES)
        scores[3:6] = [S(-200, 0, 600, 400)] * 3  # 2. Qh5 (White, the opponent) drops White to 30 %
        self.assertEqual([c.played for c in find_candidates([(g, scores)], self.cfg)], ["g8f6"])
        both = find_candidates([(g, scores)], Config(include_opponent=True))
        self.assertEqual(sorted((c.played, c.mine) for c in both), [("d1h5", False), ("g8f6", True)])
        opp = next(c for c in both if not c.mine)
        self.assertAlmostEqual(opp.score, opp.loss * 0.8)  # own mistakes rank first at equal loss

    def test_opponent_card_comment_and_tag(self):
        table = {fen_key(board_at(SCHOLAR_MOVES, 2).fen()): Eval(20, [
            Line(S(30, 100, 850, 50), ["g1f3", "b8c6"]), Line(S(-200, 0, 600, 400), ["d1h5", "b8c6"])])}
        card = build_card(board_at(SCHOLAR_MOVES, 2), 2, "d1h5", FakeAnalyser(table=table), self.cfg, "Opponent")
        self.assertFalse(card.mine)
        self.assertTrue(card.comment.startswith("Opponent played Qh5 here, losing 22% win rate."))
        note = notes.build(scholar_game(), SCHOLAR_SCORES, [card])
        self.assertIn("knightly::opponent", note.tags)

    def test_recurring_positions_rank_higher(self):
        a = scholar_game()
        b = scholar_game(id="test0002")
        one = find_candidates([(a, SCHOLAR_SCORES)], self.cfg)[0]
        two = find_candidates([(a, SCHOLAR_SCORES), (b, SCHOLAR_SCORES)], self.cfg)
        self.assertEqual(len(two), 1)
        self.assertEqual(two[0].recurrences, 2)
        self.assertGreater(two[0].score, one.score)

    def test_build_card(self):
        card = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(), self.cfg)
        self.assertEqual(card.played.nag, BLUNDER)
        self.assertEqual([a.move for a in card.alternatives], ["g7g6", "d8e7", "d8f6"])
        self.assertEqual([a.nag for a in card.alternatives], [GOOD, GOOD, GOOD])  # three good moves, no sacrifice
        self.assertEqual(card.alternatives[0].reply, ["h5f3"])
        self.assertIn("g6 was best", card.comment)
        self.assertIn("loses 52% win rate", card.comment)

    def test_build_card_drops_false_alarm(self):
        table = {PUZZLE_FEN: Eval(20, [Line(S(-30, 50, 850, 100), ["g7g6"]), Line(S(-25, 70, 850, 80), ["g8f6"])])}
        self.assertIsNone(build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(table=table), self.cfg))


class NotesTest(unittest.TestCase):
    def _note(self, existing=None):
        g = scholar_game()
        card = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(), Config())
        return notes.build(g, SCHOLAR_SCORES, [card], existing)

    def test_golden_pgn(self):
        """The card UI's tests parse this file too (card/test/generated.test.ts)."""
        n = self._note()
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.write_text(n.pgn + "\n")
        self.assertEqual(n.pgn + "\n", GOLDEN.read_text())
        self.assertEqual(n.cards, "{{c1::1::1}}")
        self.assertIn("knightly::game::lichess-test0001", n.tags)
        self.assertNotIn("#0", n.pgn)
        self.assertNotIn("100.00", n.pgn)

    def test_ids_are_stable(self):
        n = self._note(existing={"other-position": 1, PUZZLE_FEN: 4})
        self.assertEqual(n.cards, "{{c4::4::4}}")
        self.assertEqual(notes.card_ids(n.pgn), {PUZZLE_FEN: 4})
        self.assertEqual(n.new_cards, 0)

    def test_field_roundtrip(self):
        pgn = self._note().pgn
        self.assertEqual(notes.field_to_pgn(notes.pgn_to_field(pgn)), pgn)


class JobsTest(unittest.TestCase):
    def test_run_update_without_fetch(self):
        c = tmpcache(self)
        g = scholar_game(end_ts=int(__import__("time").time()) - 3600)
        c.put_games([g], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused")
        res = run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual((res.analysed_games, res.new_cards, len(res.notes)), (1, 1, 1))
        self.assertIsNotNone(c.get_analysis(g))
        # Second run: the position is on a card already, so nothing new.
        again = run_update(cfg, c, fetch=False, existing={g.key: res.notes[0].pgn}, analyser_factory=FakeAnalyser)
        self.assertEqual((again.analysed_games, again.new_cards, again.notes), (0, 0, []))

    def test_newest_games_first_across_sites(self):
        import time as _t
        c = tmpcache(self)
        now = int(_t.time())
        old_li = scholar_game(id="old", end_ts=now - 7200)
        new_cc = scholar_game(source="chesscom", id="new", end_ts=now - 60)
        c.put_games([old_li], "knightlyme")
        c.put_games([new_cc], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", chesscom_user="KnightlyMe", stockfish_path="x", max_games_per_run=1)
        run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertIsNotNone(c.get_analysis(new_cc))
        self.assertIsNone(c.get_analysis(old_li))

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        c = tmpcache(self)
        c.put_games([scholar_game(end_ts=int(__import__("time").time()))], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused")

        class Cancelling(FakeAnalyser):
            def analyse_game(self, game, progress=None):
                raise http.Cancelled()

        self.assertTrue(run_update(cfg, c, fetch=False, cancel=ev, analyser_factory=Cancelling).cancelled)


class AnalyserTest(unittest.TestCase):
    """Combining Stockfish and Lc0, with the engine processes replaced by scripted answers."""

    def analyser(self, sf: Eval, lc: Eval, use_lc0=True):
        from knightly.engine import analyse

        cfg = Config(stockfish_path="sf", use_lc0=use_lc0, lc0_path="lc0")
        an = analyse.Analyser(cfg, tmpcache(self))
        self.addCleanup(an.close)
        an.sf.evaluate = lambda board, n, moves=None: sf
        if an.lc0:
            an.lc0.evaluate = lambda board, n, moves=None: lc
        return an

    SF = Eval(13, [Line(S(40, 300, 650, 50), ["e2e4"])], "stockfish")
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

    def test_gate_pauses_analysis(self):
        an = self.analyser(self.SF, self.LC, use_lc0=False)
        an.gate = threading.Event()  # cleared: paused
        done = []
        t = threading.Thread(target=lambda: done.append(an.evaluate(chess.Board())))
        t.start()
        t.join(0.6)
        self.assertEqual(done, [])  # still waiting
        an.gate.set()
        t.join(5)
        self.assertEqual(done, [self.SF])

    def test_live_query_rejects_illegal_moves(self):
        from knightly.live import query

        an = self.analyser(self.SF, self.LC, use_lc0=False)
        with self.assertRaises(ValueError):
            query(an, chess.STARTING_FEN, 1, ["e2e5"])
        self.assertEqual(query(an, chess.STARTING_FEN, 1),
                         [{"uci": "e2e4", "pv": ["e2e4"], "wdl": [300, 650, 50], "cp": 40, "mate": None}])

    def test_stockfish_alone(self):
        an = self.analyser(self.SF, self.LC, use_lc0=False)
        self.assertIsNone(an.lc0)
        self.assertIs(an.evaluate(chess.Board()), self.SF)

    def test_limits(self):
        from knightly.engine.analyse import Engine

        cfg = Config(sf_depth=22, lc0_time=0.75)
        self.assertEqual(Engine("stockfish", "x", cfg, None).limit().depth, 22)
        self.assertEqual(Engine("lc0", "x", cfg, None).limit().time, 0.75)
        self.assertEqual((Engine("stockfish", "x", cfg, None).effort, Engine("lc0", "x", cfg, None).effort), (22, 750))

    def test_old_engine_path_setting(self):
        self.assertEqual(Config.from_dict({"engine_path": "/usr/bin/stockfish"}).stockfish_path, "/usr/bin/stockfish")
        self.assertEqual(Config.from_dict({"engine_path": "a", "stockfish_path": "b"}).stockfish_path, "b")


class EngineTest(unittest.TestCase):
    def test_pick_asset(self):
        assets = [{"name": n} for n in (
            "stockfish-linux-x86-64-universal.tar.gz", "stockfish-linux-arm64-universal.tar.gz",
            "stockfish-macos-universal.tar.gz", "stockfish-windows-x86-64-universal.zip")]
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
            p.write_text(f"#!{sys.executable}\nimport sys\nfor l in sys.stdin:\n"
                         f"    if l.strip()=='uci': print('id name {name}'); {opt}print('uciok'); sys.stdout.flush()\n"
                         "    if l.strip()=='quit': break\n")
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

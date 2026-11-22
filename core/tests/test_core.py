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

from helpers import (FIXTURES, FRENCH, PUZZLE_FEN, SCHOLAR_MOVES, SCHOLAR_SCORES, SICILIAN, FakeAnalyser,
                     FakeResponse, FakeSession, S, ScriptedAnalyser, board_at, game_from, scholar_game, scores_with)
from knightly import deepen, http, notes, openings, phases, sources
from knightly.cache import SqliteCache, fen_key
from knightly.config import Config
from knightly.engine import discover, install
from knightly.jobs import opening_order, remake_notes, run_update
from knightly.model import Eval, Game, Line, NoWdl, Score
from knightly.select import (BLUNDER, BRILLIANT, DUBIOUS, GOOD, INTERESTING, MISTAKE, NEUTRAL, SINGULAR, Graded, build_card,
                             find_candidates, game_note_key, grade, line_material, opening_candidates,
                             opening_note_key, parse_game_note_key, parse_opening_note_key, ranked_candidates)
from knightly.sources import chesscom, lichess

GOLDEN = FIXTURES / "generated_note.pgn"


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

    def test_all_time_backfill(self):
        c = tmpcache(self)
        base = "https://api.chess.com/pub/player/knightlyme/games/"
        arch = {"archives": [base + "2019/03", base + "2026/09", base + "2026/10"]}
        s = FakeSession([
            ("/games/archives", FakeResponse(200, arch)),
            ("/games/", FakeResponse(200, {"games": []})),
        ])
        now = 1793000000  # 2026-10-26
        months = lambda: [u.rsplit("games/", 1)[1] for u, _ in s.requests if "/games/2" in u]  # noqa: E731
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
        self.assertEqual(self.g([("takes_knight", 0, 3), ("takes_queen", 50, 9)]),
                         {"takes_knight": SINGULAR, "takes_queen": BLUNDER})
        # Giving back less than the material you're up still counts against the current balance.
        self.assertEqual(self.g([("a", 0, 2), ("b", 30, 5)], current=5), {"a": BRILLIANT, "b": BLUNDER})

    def test_interesting_and_good(self):
        nags = self.g([("sac", 0, -3), ("safe", 0.8, 0), ("bad", 8, 0)])
        self.assertEqual(nags, {"sac": INTERESTING, "safe": GOOD, "bad": DUBIOUS})
        # Two good moves that sacrifice the same: neither has a good alternative keeping more material.
        self.assertEqual(self.g([("x", 0, -3), ("y", 1, -3), ("greedy", 25, 0)]),
                         {"x": GOOD, "y": GOOD, "greedy": BLUNDER})

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
            return {m.move: m.nag for m in grade([Graded(mv, loss, 0, mate=mate) for mv, loss, mate in moves], legal, self.cfg)}
        # Any forced mate is good, however long; a won position without the mate is not.
        self.assertEqual(g([("m3", 0, 3), ("m5", 0, 5), ("won", 0, None), ("worse", 15, None)]),
                         {"m3": GOOD, "m5": GOOD, "won": DUBIOUS, "worse": MISTAKE})
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
        table = {fen_key(chess.Board("4k3/8/8/3P4/8/8/8/4K3 b - - 0 1").fen()):
                 Eval(20, [Line(S(0, 0, 1000, 0), ["e8d7", "e1e2", "d7d6", "e2d3"])])}
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

    def test_opponent_card_comment_and_tag(self):
        table = {fen_key(board_at(SCHOLAR_MOVES, 2).fen()): Eval(20, [
            Line(S(30, 100, 850, 50), ["g1f3", "b8c6"]), Line(S(-200, 0, 600, 400), ["d1h5", "b8c6"])])}
        card = build_card(board_at(SCHOLAR_MOVES, 2), 2, "d1h5", FakeAnalyser(table=table), self.cfg, "Opponent")
        self.assertFalse(card.mine)
        self.assertTrue(card.comment.startswith("Opponent played Qh5 here, losing 22% win rate."))
        note = notes.build(scholar_game(), SCHOLAR_SCORES, [card])
        self.assertIn("knightly::opponent", note.tags)

    def test_game_by_game_newest_first(self):
        old = game_from(SICILIAN, white=False, id="old", end_ts=100)
        new = game_from(FRENCH, white=False, id="new", end_ts=200)
        analysed = [(old, scores_with({3: 15, 5: 30}, len(SICILIAN))), (new, scores_with({1: 12, 3: 11}, len(FRENCH)))]
        cands = find_candidates(analysed, self.cfg)
        # Every qualifying move of the newest game first, in ply order, then the older game's.
        self.assertEqual([(c.game_key, c.ply) for c in cands],
                         [("lichess:new", 1), ("lichess:new", 3), ("lichess:old", 3), ("lichess:old", 5)])
        self.assertEqual({c.note for c in cands}, {"lichess:new:opening", "lichess:old:opening"})

    def test_ranked_mode_weights(self):
        g = scholar_game()
        scores = list(SCHOLAR_SCORES)
        scores[3:6] = [S(-200, 0, 600, 400)] * 3  # 2. Qh5 (White, the opponent) drops White to 30 %
        both = ranked_candidates([(g, scores)], Config(include_opponent=True))
        self.assertEqual(sorted((c.played, c.mine) for c in both), [("d1h5", False), ("g8f6", True)])
        opp = next(c for c in both if not c.mine)
        self.assertAlmostEqual(opp.score, opp.loss * 0.8)  # own mistakes rank first at equal loss
        self.assertEqual(both[0].note, g.key + ":opening")
        self.assertEqual(ranked_candidates([(g, SCHOLAR_SCORES)], self.cfg, exclude={PUZZLE_FEN}), [])

    def test_ranked_mode_recurring_positions_rank_higher(self):
        a = scholar_game()
        b = scholar_game(id="test0002")
        one = ranked_candidates([(a, SCHOLAR_SCORES)], self.cfg)[0]
        two = ranked_candidates([(a, SCHOLAR_SCORES), (b, SCHOLAR_SCORES)], self.cfg)
        self.assertEqual(len(two), 1)
        self.assertEqual(two[0].recurrences, 2)
        self.assertGreater(two[0].score, one.score)

    def test_same_position_offered_once_from_newest_game(self):
        a = scholar_game(end_ts=100)
        b = scholar_game(id="test0002", end_ts=200)
        two = find_candidates([(a, SCHOLAR_SCORES), (b, SCHOLAR_SCORES)], self.cfg)
        self.assertEqual([c.game_key for c in two], ["lichess:test0002"])

    def test_build_card(self):
        card = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(), self.cfg)
        self.assertEqual(card.played.nag, BLUNDER)
        self.assertEqual([a.move for a in card.alternatives], ["g7g6", "d8e7", "d8f6"])
        self.assertEqual([a.nag for a in card.alternatives], [GOOD, GOOD, GOOD])  # three good moves, no sacrifice
        self.assertEqual(card.alternatives[0].reply, ["h5f3"])
        self.assertIn("g6 was best", card.comment)
        self.assertIn("loses 52% win rate", card.comment)

    def test_build_card_neutral_move_makes_no_puzzle(self):
        # The played move loses 3 %: normal play (no NAG), not a mistake.
        table = {PUZZLE_FEN: Eval(20, [Line(S(-30, 50, 850, 100), ["g7g6"]), Line(S(-25, 70, 850, 80), ["g8f6"])])}
        self.assertIsNone(build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(table=table), self.cfg, min_loss=1))

    def test_build_card_drops_false_alarm(self):
        table = {PUZZLE_FEN: Eval(20, [Line(S(-30, 50, 850, 100), ["g7g6"]), Line(S(-25, 70, 850, 80), ["g8f6"])])}
        self.assertIsNone(build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(table=table), self.cfg))


class PhasesTest(unittest.TestCase):
    def test_matches_lichess(self):
        for gid, moves, division in division_games():
            self.assertEqual(phases.divide(moves), division, gid)

    def test_phase_at(self):
        self.assertEqual(phases.divide([]), (None, None))
        self.assertEqual(phases.divide(SCHOLAR_MOVES), (None, None))
        self.assertEqual([phases.phase_at((16, 40), p) for p in (0, 15, 16, 39, 40)],
                         ["opening", "opening", "middlegame", "middlegame", "endgame"])
        self.assertEqual(phases.phase_at((None, None), 80), "opening")

    def test_endgame_needs_a_middlegame_before_it(self):
        start = chess.Board()
        bare = chess.Board("4k3/pppppppp/8/8/8/8/PPPPPPPP/4K3 w - - 0 1")  # no pieces: both at once
        self.assertEqual(phases.divide_boards([start, bare]), (None, 1))
        self.assertEqual(phases.phase_at((None, 1), 1), "endgame")


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
        self.assertEqual(n.key, "lichess:test0001:opening")
        self.assertTrue(n.notes.startswith("Opening · <a href"))
        self.assertEqual(n.tags, ["knightly", "knightly::game::lichess-test0001::opening", "knightly::color::black", "knightly::motif::other",
                                  "knightly::opening::king-s-pawn-game", "knightly::phase::opening", "knightly::rated",
                                  "knightly::result::loss", "knightly::severity::blunder", "knightly::site::lichess",
                                  "knightly::time::blitz"])
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


class OpeningsTest(unittest.TestCase):
    def test_classify_and_family(self):
        eco, name = openings.classify(SICILIAN)
        self.assertEqual((eco, name), ("B90", "Sicilian Defense: Najdorf Variation"))
        self.assertEqual(openings.family(name), "Sicilian Defense")
        # A transposition (1. Nf3 c5 2. e4) still lands in the table.
        self.assertEqual(openings.classify(["g1f3", "c7c5", "e2e4"])[1], "Sicilian Defense")
        self.assertEqual(openings.classify([]), ("", "Other"))

    def test_rank_weights_results_and_splits_colour(self):
        games = [
            game_from(SICILIAN, white=False, id="s1", result="1-0"),  # loss: 4
            game_from(FRENCH, white=False, id="f1", result="0-1"),  # win: 1
            game_from(FRENCH, white=False, id="f2", result="1/2-1/2"),  # draw: 2
            game_from(SICILIAN, white=True, id="s2", result="1-0"),  # a win as White: its own group
        ]
        ranked = openings.rank(games)
        self.assertEqual([(g.label, g.importance) for g in ranked],
                         [("Sicilian Defense (Black)", 4), ("French Defense (Black)", 3), ("Sicilian Defense (White)", 1)])

    def test_opening_candidates_order(self):
        sic = game_from(SICILIAN, white=False, id="s1", result="1-0", end_ts=100)
        fr_old = game_from(FRENCH, white=False, id="f1", result="1/2-1/2", end_ts=50)
        fr_new = game_from(FRENCH + ["b1c3"], white=False, id="f2", result="0-1", end_ts=300)
        groups = openings.rank([sic, fr_old, fr_new])
        self.assertEqual([g.family for g in groups], ["Sicilian Defense", "French Defense"])
        analysed = {
            "lichess:s1": scores_with({5: 8, 3: 6, 1: 4}, len(SICILIAN)),  # 4 is normal play: no card
            "lichess:f1": scores_with({1: 6}, len(FRENCH)),
            "lichess:f2": scores_with({1: 6, 3: 5.5}, len(FRENCH) + 1),
        }
        cands = opening_candidates(groups, analysed, Config())
        self.assertEqual([(c.game_key, c.ply) for c in cands],
                         [("lichess:s1", 3), ("lichess:s1", 5), ("lichess:f2", 1), ("lichess:f2", 3)])
        # f1's 1...e6 is the same position as f2's: offered once, from the newest game.
        self.assertEqual([c.note for c in cands],
                         ["opening:black:sicilian-defense:4", "opening:black:sicilian-defense:6",
                          "opening:black:french-defense:2", "opening:black:french-defense:4"])
        self.assertEqual(parse_opening_note_key(cands[0].note), ("black", "sicilian-defense", 3))
        self.assertIsNone(parse_opening_note_key("lichess:abcd"))
        # Already-carded positions are skipped; the opponent's moves come in when asked for.
        skip = {cands[0].fen_key}
        self.assertNotIn(cands[0].fen_key, [c.fen_key for c in opening_candidates(groups, analysed, Config(), skip)])
        analysed["lichess:s1"] = scores_with({2: 9}, len(SICILIAN))
        self.assertEqual([c.ply for c in opening_candidates(groups[:1], analysed, Config())], [])
        both = opening_candidates(groups[:1], analysed, Config(include_opponent=True))
        self.assertEqual([(c.ply, c.mine) for c in both], [(2, False)])

    def test_note_tags_roundtrip(self):
        for key in ("lichess:abcd1234:opening", "chesscom:123:endgame", "opening:black:sicilian-defense:10"):
            self.assertEqual(notes.key_from_tags(["knightly", notes.note_key_tag(key)]), key)
        self.assertEqual(notes.note_key_tag("lichess:abcd1234:middlegame"), "knightly::game::lichess-abcd1234::middlegame")
        self.assertIsNone(notes.key_from_tags(["knightly::game::lichess-abcd1234"]))  # from before phases
        self.assertEqual(parse_game_note_key(game_note_key("lichess:x", "endgame")), ("lichess:x", "endgame"))
        self.assertIsNone(parse_game_note_key("opening:black:sicilian-defense:10"))
        self.assertIsNone(parse_game_note_key("lichess:abcd1234"))
        self.assertEqual(notes.note_key_tag("opening:white:kings-pawn-game:7"),
                         "knightly::by-opening::white-kings-pawn-game::ply-007")

    def test_order(self):
        groups = openings.rank([game_from(SICILIAN, white=False, id="a", result="1-0"),
                                game_from(FRENCH, white=False, id="b", result="0-1")])
        keys = {"opening:black:french-defense:2", "opening:black:sicilian-defense:6", "lichess:x",
                "opening:black:sicilian-defense:2", "opening:white:gone:1"}
        self.assertEqual(opening_order(groups, keys),
                         ["opening:black:sicilian-defense:2", "opening:black:sicilian-defense:6",
                          "opening:black:french-defense:2", "opening:white:gone:1"])

    def test_build_opening_appends_games(self):
        g1 = scholar_game(id="g1")
        card = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(), Config())
        key = "opening:black:kings-pawn-game:6"
        n1 = notes.build_opening(key, "King's Pawn Game (Black)", 5, [(g1, SCHOLAR_SCORES, card)])
        self.assertEqual((n1.cards, n1.new_cards), ("{{c1::1::1}}", 1))
        self.assertIn("knightly::by-opening::black-kings-pawn-game::ply-006", n1.tags)
        self.assertIn("knightly::color::black", n1.tags)
        self.assertTrue(n1.notes.startswith("King&#x27;s Pawn Game (Black), move 3… (ply 6)"))
        # A second game, another position at the same ply: the first game stays as it was.
        g2 = game_from(FRENCH + ["b1c3", "g8f6"], white=False, id="g2", url="https://lichess.org/g2")
        sc = scores_with({5: 30}, 6)
        table = {fen_key(board_at(g2.moves, 5).fen()): Eval(20, [
            Line(S(0, 0, 1000, 0), ["f8b4"]), Line(S(600, 600, 400, 0), ["g8f6"])])}
        card2 = build_card(board_at(g2.moves, 5), 5, "g8f6", FakeAnalyser(table=table), Config(), min_loss=2)
        n2 = notes.build_opening(key, "King's Pawn Game (Black)", 5, [(g2, sc, card2)], n1.pgn)
        self.assertEqual((n2.cards, n2.new_cards), ("{{c1::1::1}} {{c2::2::2}}", 1))
        self.assertTrue(n2.pgn.startswith(n1.pgn.strip()))
        self.assertEqual(notes.card_ids(n2.pgn), {PUZZLE_FEN: 1, card2.fen_key: 2})
        self.assertEqual(n2.notes.count("<a href"), 2)
        golden = FIXTURES / "generated_opening_note.pgn"  # also parsed by card/test/generated.test.ts
        if os.environ.get("UPDATE_GOLDEN"):
            golden.write_text(n2.pgn + "\n")
        self.assertEqual(n2.pgn + "\n", golden.read_text())


class JobsTest(unittest.TestCase):
    def test_opening_mode_ply_by_ply_across_runs(self):
        import time as _t
        c = tmpcache(self)
        now = int(_t.time())
        sic = game_from(SICILIAN, white=False, id="s1", result="1-0", end_ts=now - 100)
        fr = game_from(FRENCH, white=False, id="f1", result="0-1", end_ts=now - 50)
        c.put_games([sic, fr], "knightlyme")
        script = {"s1": {5: 30, 3: 12}, "f1": {1: 20}}
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x", note_grouping="opening", max_new_cards=2)
        an = lambda *a: ScriptedAnalyser([sic, fr], script)  # noqa: E731
        res = run_update(cfg, c, fetch=False, analyser_factory=an)
        # The Sicilian (a loss) outranks the French (a win), whatever the game dates; ply 4 before ply 6.
        self.assertEqual([n.key for n in res.notes],
                         ["opening:black:sicilian-defense:4", "opening:black:sicilian-defense:6"])
        self.assertEqual(res.new_cards, 2)
        self.assertEqual(res.order, ["opening:black:sicilian-defense:4", "opening:black:sicilian-defense:6"])
        existing = {n.key: n.pgn for n in res.notes}
        again = run_update(cfg, c, fetch=False, existing=existing, analyser_factory=an)
        self.assertEqual([n.key for n in again.notes], ["opening:black:french-defense:2"])
        self.assertEqual(again.order, ["opening:black:sicilian-defense:4", "opening:black:sicilian-defense:6",
                                       "opening:black:french-defense:2"])

    def test_remake_keeps_cards_and_deprecates_lost_puzzles(self):
        import time as _t
        c = tmpcache(self)
        now = int(_t.time())
        sic = game_from(SICILIAN, white=False, id="s1", result="1-0", end_ts=now - 100)
        sic.url = "https://lichess.org/s1/black"
        c.put_games([sic], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x", max_new_cards=2)
        script = {"s1": {5: 30, 3: 12}}
        made = run_update(cfg, c, fetch=False, analyser_factory=lambda *a: ScriptedAnalyser([sic], script))
        existing = {n.key: n.pgn for n in made.notes}
        self.assertEqual(len(existing), 1)
        # Same engine, same code: nothing changes and nothing is deprecated.
        same = remake_notes(cfg, c, existing, analyser_factory=lambda *a: ScriptedAnalyser([sic], script))
        self.assertEqual((same.pgns, same.deprecated), ({}, {}))
        self.assertEqual(same.remade, same.cards)
        self.assertGreater(same.cards, 0)
        # The move is no longer a mistake: the card is deprecated and the PGN stays as it was.
        gone = remake_notes(cfg, c, existing, analyser_factory=lambda *a: ScriptedAnalyser([sic], {"s1": {}}))
        self.assertEqual(gone.remade, 0)
        self.assertEqual(list(gone.deprecated.values()), [[1, 2]])
        self.assertEqual(gone.pgns, {})

    def test_remake_adds_nags_to_the_other_moves(self):
        c = tmpcache(self)
        g = scholar_game()
        g.url = "https://lichess.org/test0001/black"
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x")
        c.put_games([g], "knightlyme")
        card = build_card(board_at(SCHOLAR_MOVES, 5), 5, "g8f6", FakeAnalyser(), cfg)
        old = notes.build(g, [None] * 8, [card])  # an old note: evals only, no NAGs on the other moves
        self.assertNotIn("$6", old.pgn)

        class Known(FakeAnalyser):
            def known(self, keys):
                return {k: (13, s) for k, s in self.scores.items() if k in keys}

        res = remake_notes(cfg, c, {old.key: old.pgn}, analyser_factory=lambda *a: Known())
        self.assertIn("Qh5 $6", res.pgns[old.key])  # 2. Qh5 lost 6 points

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
        table = {PUZZLE_FEN: Eval(depth=20, lines=[
            Line(S(-30, 50, 850, 100), ["g8f6", "h5f7"]),
            Line(S(300, 490, 500, 10), ["g7g6", "h5f3"]),
            Line(S(300, 490, 500, 10), ["d8e7", "b1c3"])])}
        g2 = scholar_game(moves=SCHOLAR_MOVES[:5] + ["g8f6"])
        self.assertEqual(game_nags(g2, FakeAnalyser(table=table), cfg), {5: 8})

    def test_game_mode_default(self):
        import time as _t
        c = tmpcache(self)
        now = int(_t.time())
        sic = game_from(SICILIAN, white=False, id="s1", result="1-0", end_ts=now - 100)
        fr = game_from(FRENCH, white=False, id="f1", result="0-1", end_ts=now - 50)
        c.put_games([sic, fr], "knightlyme")
        script = {"s1": {5: 30, 3: 12}, "f1": {1: 20}}
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x", max_new_cards=2)
        res = run_update(cfg, c, fetch=False, analyser_factory=lambda *a: ScriptedAnalyser([sic, fr], script))
        # Newest game first: the French game's card, then the Sicilian's first one.
        self.assertEqual([(n.key, n.new_cards) for n in res.notes], [("lichess:f1:opening", 1), ("lichess:s1:opening", 1)])
        self.assertEqual(res.order, [])

    def test_one_note_per_phase(self):
        import time as _t
        c = tmpcache(self)
        gid, moves, (mid, _) = next(d for d in division_games() if d[0] == "rRWljygz")
        g = game_from(moves, white=True, id=gid, result="0-1", end_ts=int(_t.time()) - 60)
        c.put_games([g], "knightlyme")
        early, late = mid - 6, mid + 4  # White's moves, either side of the middlegame start
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x", max_new_cards=5)
        an = lambda *a: ScriptedAnalyser([g], {gid: {early: 20, late: 25}})  # noqa: E731
        res = run_update(cfg, c, fetch=False, analyser_factory=an)
        by_key = {n.key: n for n in res.notes}
        self.assertEqual(set(by_key), {f"lichess:{gid}:opening", f"lichess:{gid}:middlegame"})
        for phase in ("opening", "middlegame"):
            n = by_key[f"lichess:{gid}:{phase}"]
            self.assertEqual(n.new_cards, 1)
            self.assertEqual([t for t in n.tags if "::phase::" in t], [f"knightly::phase::{phase}"])
            self.assertIn("knightly::color::white", n.tags)
            self.assertIn("knightly::result::loss", n.tags)
        self.assertIn("knightly::severity::mistake", by_key[f"lichess:{gid}:opening"].tags)
        self.assertIn("knightly::severity::blunder", by_key[f"lichess:{gid}:middlegame"].tags)
        # Next run: both notes are known, nothing new to add.
        again = run_update(cfg, c, fetch=False, existing={k: n.pgn for k, n in by_key.items()}, analyser_factory=an)
        self.assertEqual(again.new_cards, 0)

    def test_ranked_mode(self):
        import time as _t
        c = tmpcache(self)
        now = int(_t.time())
        sic = game_from(SICILIAN, white=False, id="s1", result="1-0", end_ts=now - 100)
        fr = game_from(FRENCH, white=False, id="f1", result="0-1", end_ts=now - 50)
        c.put_games([sic, fr], "knightlyme")
        script = {"s1": {5: 30, 3: 12}, "f1": {1: 20}}
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x", max_new_cards=2, note_grouping="ranked")
        res = run_update(cfg, c, fetch=False, analyser_factory=lambda *a: ScriptedAnalyser([sic, fr], script))
        # Biggest scores first, whatever the game: 30 (a loss) beats 20 × 0.6 (a win) and 12.
        self.assertEqual([(n.key, n.new_cards) for n in res.notes], [("lichess:s1:opening", 1), ("lichess:f1:opening", 1)])

    def test_motif_mode(self):
        import time as _t
        c = tmpcache(self)
        now = int(_t.time())
        sic = game_from(SICILIAN, white=False, id="s1", result="1-0", end_ts=now - 100)
        fr = game_from(FRENCH, white=False, id="f1", result="0-1", end_ts=now - 50)
        c.put_games([sic, fr], "knightlyme")
        script = {"s1": {5: 30, 3: 12}, "f1": {1: 20}}
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="x", max_new_cards=2, note_grouping="motif")
        res = run_update(cfg, c, fetch=False, analyser_factory=lambda *a: ScriptedAnalyser([sic, fr], script))
        # One note per motif, games from any opening in it, biggest mistakes first.
        self.assertEqual(res.new_cards, 2)
        self.assertTrue(all(n.key.startswith("motif:") for n in res.notes))
        self.assertEqual(res.order, [n.key for n in res.notes])
        for n in res.notes:
            self.assertIn(notes.note_key_tag(n.key), n.tags)
            self.assertTrue(any(t.startswith("knightly::motif::") for t in n.tags))
        existing = {n.key: n.pgn for n in res.notes}
        again = run_update(cfg, c, fetch=False, existing=existing, analyser_factory=lambda *a: ScriptedAnalyser([sic, fr], script))
        self.assertEqual(again.new_cards, 1)

    def test_run_update_without_fetch(self):
        c = tmpcache(self)
        g = scholar_game(end_ts=int(__import__("time").time()) - 3600)
        c.put_games([g], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused")
        res = run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual((res.new_cards, len(res.notes)), (1, 1))
        self.assertGreater(res.analysed_positions, 0)
        # Second run: the position is on a card already, so nothing new.
        again = run_update(cfg, c, fetch=False, existing={g.key: res.notes[0].pgn}, analyser_factory=FakeAnalyser)
        self.assertEqual((again.new_cards, again.notes), (0, []))

    def test_skips_when_nothing_changed(self):
        import time as _t
        c = tmpcache(self)
        g = scholar_game(end_ts=int(_t.time()) - 3600)
        c.put_games([g], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused")
        calls = []
        def factory(*a):
            calls.append(1)
            return FakeAnalyser(*a)
        run = lambda cfg=cfg, **kw: run_update(cfg, c, fetch=False, analyser_factory=factory, **kw)  # noqa: E731
        first = run()
        existing = {g.key: first.notes[0].pgn}
        self.assertFalse(run().skipped)  # cards were made last time and may never have been written
        run(existing=existing)  # settles: the new card is on the collection now
        n = len(calls)
        again = run(existing=existing)
        self.assertTrue(again.skipped)
        self.assertEqual((len(calls), again.notes, again.analysed_positions), (n, [], 0))
        self.assertFalse(run(existing=existing, force=True).skipped)
        # A deleted card frees its position, a setting change or a new game changes the inputs.
        self.assertFalse(run().skipped)
        self.assertFalse(run(Config(lichess_user="KnightlyMe", stockfish_path="unused", min_loss=20), existing=existing).skipped)
        c.put_games([scholar_game(end_ts=int(_t.time()) - 60, id="other")], "knightlyme")
        self.assertFalse(run(existing=existing).skipped)

    def test_on_note_gets_each_note_as_its_card_is_built(self):
        import time as _t
        c = tmpcache(self)
        c.put_games([scholar_game(end_ts=int(_t.time()) - 3600)], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused")
        seen = []
        res = run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser, on_note=seen.append)
        self.assertEqual(res.new_cards, 1)
        self.assertEqual(seen, res.notes)
        # A cancel after the first note keeps it in the result.
        c2 = tmpcache(self)
        c2.put_games([scholar_game(end_ts=int(_t.time()) - 3600)], "knightlyme")
        ev = threading.Event()

        def stop(spec):
            ev.set()

        res = run_update(cfg, c2, fetch=False, analyser_factory=FakeAnalyser, on_note=stop, cancel=ev)
        self.assertEqual(len(res.notes), 1)

    def test_precalculate_saves_candidates_for_the_next_update(self):
        import time as _t
        c = tmpcache(self)
        g = scholar_game(end_ts=int(_t.time()) - 3600)
        c.put_games([g], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused")
        pre = run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser, precalculate=True)
        self.assertEqual((pre.new_cards, pre.notes, pre.skipped), (0, [], False))  # candidates only, no cards
        self.assertTrue(c.get("confirmed")["confirmed"])
        saved = {tuple(x) for x in c.get("confirmed")["confirmed"]}
        res = run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual(res.new_cards, 1)  # made from the saved candidates
        self.assertTrue(saved <= {tuple(x) for x in c.get("confirmed")["confirmed"]})
        # A change that affects which moves are mistakes drops the saved candidates.
        from knightly.jobs import precalc_signature
        self.assertNotEqual(precalc_signature(cfg), precalc_signature(Config(lichess_user="KnightlyMe", stockfish_path="unused", min_loss=20)))
        self.assertEqual(precalc_signature(cfg), precalc_signature(Config(lichess_user="x", stockfish_path="unused", max_new_cards=99)))

    def test_unfinished_run_is_not_skipped(self):
        import time as _t
        c = tmpcache(self)
        c.put_games([scholar_game(end_ts=int(_t.time()) - 3600)], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused", analysis_minutes=1e-9)
        run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser)  # budget ran out at once
        self.assertFalse(run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser).skipped)

    def test_progress_events(self):
        c = tmpcache(self)
        c.put_games([scholar_game(end_ts=int(__import__("time").time()))], "knightlyme")
        events = []
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused", max_new_cards=3)
        run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser, progress=lambda *e: events.append(e))
        pos = [(d, t) for s, d, t, _ in events if s == "positions"]
        self.assertTrue(pos)
        self.assertTrue(all(0 <= d <= t for d, t in pos))
        self.assertTrue(all(t == 3 for s, _, t, _ in events if s == "analyse" and t))

    def test_newest_game_gets_a_shared_mistake(self):
        import time as _t
        c = tmpcache(self)
        now = int(_t.time())
        old_li = scholar_game(id="old", end_ts=now - 7200)
        new_cc = scholar_game(source="chesscom", id="new", end_ts=now - 60)
        c.put_games([old_li], "knightlyme")
        c.put_games([new_cc], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", chesscom_user="KnightlyMe", stockfish_path="x")
        res = run_update(cfg, c, fetch=False, analyser_factory=FakeAnalyser)
        self.assertEqual([n.key for n in res.notes], [new_cc.key + ":opening"])

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        c = tmpcache(self)
        c.put_games([scholar_game(end_ts=int(__import__("time").time()))], "knightlyme")
        cfg = Config(lichess_user="KnightlyMe", stockfish_path="unused")

        class Cancelling(FakeAnalyser):
            def score(self, board, depth):
                raise http.Cancelled()

        self.assertTrue(run_update(cfg, c, fetch=False, cancel=ev, analyser_factory=Cancelling).cancelled)


class DeepenTest(unittest.TestCase):
    """Analysis by increasing depth (deepen.py)."""

    def setUp(self):
        self.sic = game_from(SICILIAN, white=False, id="s1", end_ts=200)
        self.fr = game_from(FRENCH, white=False, id="f1", end_ts=100)
        self.cfg = Config(sf_depth=3, max_new_cards=5)

    def run_deepener(self, script, **kw):
        an = ScriptedAnalyser([self.sic, self.fr], script)
        p = deepen.collect([self.sic, self.fr], self.cfg)
        dp = deepen.Deepener(p, an, self.cfg, threshold=self.cfg.min_loss, card_min_loss=None, opponents={},
                             want=self.cfg.max_new_cards, **kw)
        dp.run()
        return p, an, dp

    def test_collect_counts_games(self):
        p = deepen.collect([self.sic, self.fr], self.cfg)
        start, after_e4 = p.keys["lichess:s1"][:2]
        self.assertEqual((p.count[start], p.count[after_e4]), (2, 2))
        self.assertEqual(p.order[:2], [start, after_e4])  # most-seen first
        self.assertTrue(all(e.mine for e in p.edges))  # Black's moves only
        self.assertEqual(len(p.edges), 5 + 2)

    def test_depth_one_first_most_seen_first(self):
        p, an, dp = self.run_deepener({})
        first = len(p.fen)
        self.assertEqual({d for _, d in an.searched[:first]}, {1})
        self.assertEqual([k for k, _ in an.searched[:2]], p.order[:2])
        # No mistakes: everything is deepened to the configured depth in the end.
        self.assertTrue(all(dp.depth[k] >= 3 for k in p.fen))
        self.assertEqual(dp.confirmed, set())

    def test_candidates_climb_first(self):
        p, an, dp = self.run_deepener({"s1": {5: 30}})
        ks = p.keys["lichess:s1"]
        depth2 = [k for k, d in an.searched if d == 2]
        self.assertEqual(set(depth2[:2]), {ks[5], ks[6]})
        self.assertEqual(dp.confirmed, {(ks[5], SICILIAN[5])})

    def test_time_budget(self):
        t = [0.0]

        class Slow(ScriptedAnalyser):
            def score(self, board, depth):
                t[0] += 10  # seconds per position
                return super().score(board, depth)

        cfg = Config(sf_depth=3, analysis_minutes=1)
        an = Slow([self.sic, self.fr], {"s1": {5: 30}})
        p = deepen.collect([self.sic, self.fr], cfg)
        dp = deepen.Deepener(p, an, cfg, threshold=cfg.min_loss, card_min_loss=None, opponents={}, want=5,
                             clock=lambda: t[0])
        dp.run()
        self.assertEqual(dp.analysed, 6)
        self.assertEqual(dp.confirmed, set())


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

            def analyse(self, board, limit, multipv, root_moves=None):
                Proc.calls += 1
                return [{"depth": limit.depth, "pv": [chess.Move.from_uci("e2e4")],
                         "score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
                         "wdl": chess.engine.PovWdl(chess.engine.Wdl(300, 650, 50), chess.WHITE)}]

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

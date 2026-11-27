"""The extract stage: accounts and roles, the players index, PGN inputs and the PGN export."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import chess.pgn
from helpers import game_from, scholar_game
from knightly import http
from knightly.cache import SqliteCache
from knightly.config import ME, OTHER, Account, Config
from knightly.export import game_pgn
from knightly.sources import pgn
from knightly.stages import extract

FRENCH = ["e2e4", "e7e6", "d2d4", "d7d5"]

PGN_TEXT = """[Event "Club championship"]
[Site "?"]
[Date "2024.03.??"]
[White "Somebody"]
[Black "KnightlyMe"]
[Result "0-1"]
[TimeControl "600+5"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0

[Event "Rated Blitz game"]
[Site "https://lichess.org/abcdefgh"]
[UTCDate "2025.01.02"]
[UTCTime "10:20:30"]
[White "Carlsen, Magnus"]
[Black "Nakamura, Hikaru"]
[Result "1/2-1/2"]
[TimeControl "180+0"]

1. d4 d5 1/2-1/2

[Event "From a position"]
[SetUp "1"]
[FEN "8/8/8/8/8/8/4K3/4k3 w - - 0 1"]
[Result "*"]

1. Kd3 *
"""


def tmpdir(test: unittest.TestCase) -> Path:
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    return Path(d.name)


def cache_in(test: unittest.TestCase, folder: Path) -> SqliteCache:
    c = SqliteCache(folder / "c.sqlite")
    test.addCleanup(c.close)
    return c


def offline(test: unittest.TestCase) -> None:
    """No downloads from the sites: only the PGN inputs and the games already stored."""
    p = mock.patch.object(extract, "fetch_account", return_value=0)
    p.start()
    test.addCleanup(p.stop)


def cfg(accounts, **extract) -> Config:
    return Config.from_dict({"extract": {"accounts": accounts, "history_days": 0, **extract}})


class AccountsTest(unittest.TestCase):
    def test_old_usernames_become_your_accounts(self):
        c = Config.from_dict({"lichess_user": "a", "chesscom_user": "b"})
        self.assertEqual(c.extract.accounts, [Account("lichess", "a", ME), Account("chesscom", "b", ME)])

    def test_accounts_win_over_old_usernames(self):
        c = Config.from_dict({"lichess_user": "a", "accounts": [{"source": "lichess", "name": "x", "role": OTHER}]})
        self.assertEqual(c.extract.accounts, [Account("lichess", "x", OTHER)])

    def test_duplicates_blank_names_and_unknown_values_are_dropped(self):
        c = cfg(
            [
                {"source": "lichess", "name": "A"},
                {"source": "lichess", "name": " a "},
                {"source": "lichess", "name": ""},
                {"source": "fide", "name": "x"},
                {"source": "chesscom", "name": "y", "role": "coach"},
            ]
        )
        self.assertEqual(c.extract.accounts, [Account("lichess", "A", ME)])

    def test_accounts_survive_the_flat_form(self):
        c = cfg([{"source": "pgn", "name": "Carlsen, Magnus", "role": OTHER}], pgn_paths=["/x"])
        self.assertEqual(Config.from_dict(c.flat()), c)
        self.assertNotEqual(c.fingerprint("extract"), Config().fingerprint("extract"))

    def test_contact_goes_in_the_user_agent(self):
        self.assertIn("contact: me@example.com", http.session(contact="me@example.com").headers["User-Agent"])
        self.assertNotIn("contact", http.session().headers["User-Agent"])


class FetchTest(unittest.TestCase):
    def test_yours_first_one_failing_does_not_stop_the_others(self):
        c = cache_in(self, tmpdir(self))
        config = cfg(
            [
                {"source": "chesscom", "name": "rival", "role": OTHER},
                {"source": "lichess", "name": "me1"},
                {"source": "chesscom", "name": "me2"},
            ],
            contact_email="me@example.com",
        )
        calls = []

        def site(name):
            def fetch(cache, user, **kw):
                calls.append((name, user, kw.get("session") and kw["session"].headers["User-Agent"]))
                if user == "me1":
                    raise ValueError("down")
                return 2

            return fetch

        with (
            mock.patch.object(extract.lichess, "fetch", site("lichess")),
            mock.patch.object(extract.chesscom, "fetch", site("chesscom")),
        ):
            got = extract.run(config, c)
        self.assertEqual(
            [(s, u) for s, u, _ in calls], [("lichess", "me1"), ("chesscom", "me2"), ("chesscom", "rival")]
        )
        self.assertIn("contact: me@example.com", calls[1][2])
        self.assertEqual((got.new_games, got.warnings), (4, ["Lichess (me1): down"]))


class PlayersIndexTest(unittest.TestCase):
    def test_a_game_is_found_by_either_player(self):
        c = cache_in(self, tmpdir(self))
        g = scholar_game()  # Opponent – KnightlyMe
        c.put_games([g], "opponent")
        c.put_games([g], "knightlyme")  # fetched again for the other account: stored once
        self.assertEqual([x.key for x in c.games("lichess", "KnightlyMe")], [g.key])
        self.assertEqual([x.key for x in c.games("lichess", "opponent")], [g.key])
        self.assertEqual(c.games("chesscom", "KnightlyMe"), [])

    def test_games_stored_before_the_index_are_indexed(self):
        d = tmpdir(self)
        c = cache_in(self, d)
        g = scholar_game()
        with closing(sqlite3.connect(d / "c.sqlite")) as db, db:  # as an older version stored it
            db.execute(
                "INSERT INTO games (source, id, user, end_ts, data) VALUES (?,?,?,?,?)",
                (g.source, g.id, "knightlyme", g.end_ts, json.dumps(g.to_json())),
            )
            db.execute("DELETE FROM kv WHERE key='players_indexed'")
        c.close()
        again = cache_in(self, d)
        self.assertEqual([x.key for x in again.games("lichess", "opponent")], [g.key])


class PgnTest(unittest.TestCase):
    def test_games_are_read_once_and_skipped_when_not_from_the_start(self):
        d = tmpdir(self)
        (d / "in").mkdir()
        (d / "in" / "games.PGN").write_text(PGN_TEXT)
        (d / "in" / "notes.txt").write_text("not a pgn")
        c = cache_in(self, d)
        self.assertEqual(pgn.fetch(c, [str(d / "in")]), 2)
        self.assertEqual(pgn.fetch(c, [str(d / "in")]), 0)  # unchanged: not read again
        games = {g.white: g for g in c.games_from("pgn")}
        club, master = games["Somebody"], games["Carlsen, Magnus"]
        self.assertEqual((club.time_class, master.time_class), ("rapid", "blitz"))
        self.assertEqual(club.end_ts, 1709251200)  # 2024-03-01: an unknown day counts as the first
        self.assertEqual(master.end_ts, 1735813230)
        self.assertEqual((master.url, master.rated, club.url), ("https://lichess.org/abcdefgh", True, ""))
        # The same game in another file is stored once.
        (d / "copy.pgn").write_text(PGN_TEXT)
        self.assertEqual(pgn.fetch(c, [str(d / "in"), str(d / "copy.pgn")]), 0)

    def test_the_export_folder_is_not_read(self):
        d = tmpdir(self)
        (d / "out").mkdir()
        (d / "out" / "lichess-me.pgn").write_text(PGN_TEXT)
        (d / "mine.pgn").write_text(PGN_TEXT)
        self.assertEqual(pgn.files([str(d)], skip=str(d / "out")), [d / "mine.pgn"])

    def test_time_classes(self):
        self.assertEqual(
            [pgn.time_class(t) for t in ("15+0", "60+0", "300+3", "900+10", "5400+30", "-", "1/86400", "", "x")],
            ["ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence", "correspondence", "", ""],
        )


class SideTest(unittest.TestCase):
    def setUp(self):
        offline(self)
        self.c = cache_in(self, tmpdir(self))
        self.mine = game_from(FRENCH, white=True, id="m1", black="Rival")  # KnightlyMe – Rival
        self.rival = scholar_game(moves=FRENCH, id="r1", white="Stranger", black="Rival")
        self.c.put_games([self.mine, self.rival])
        self.cfg = cfg(
            [{"source": "lichess", "name": "KnightlyMe"}, {"source": "lichess", "name": "rival", "role": OTHER}]
        )

    def test_your_side_wins_in_a_game_against_another_account(self):
        self.assertEqual(extract.side(self.mine, self.cfg.extract), (True, ME))
        self.assertEqual(extract.side(self.rival, self.cfg.extract), (False, OTHER))

    def test_games_of_a_role(self):
        self.assertEqual([g.id for g in extract.games_of(self.cfg, self.c)], ["m1"])
        others = extract.games_of(self.cfg, self.c, OTHER)
        self.assertEqual([(g.id, g.user_is_white) for g in others], [("r1", False)])

    def test_a_site_account_does_not_match_another_sites_game(self):
        g = game_from(FRENCH, white=True, id="c1", source="chesscom")
        self.assertIsNone(extract.side(g, self.cfg.extract))

    def test_pgn_games_match_any_accounts_name(self):
        d = tmpdir(self)
        (d / "g.pgn").write_text(PGN_TEXT)
        c = cfg(
            [{"source": "lichess", "name": "KnightlyMe"}, {"source": "pgn", "name": "Nakamura, Hikaru", "role": OTHER}],
            pgn_paths=[str(d / "g.pgn")],
        )
        got = extract.run(c, self.c)
        self.assertEqual(got.new_games, 2)
        mine = [g for g in extract.games_of(c, self.c) if g.source == "pgn"]
        self.assertEqual([(g.white, g.user_is_white) for g in mine], [("Somebody", False)])
        self.assertEqual([g.black for g in extract.games_of(c, self.c, OTHER)], ["Nakamura, Hikaru"])

    def test_filters_apply_to_site_games_only(self):
        c = cfg([{"source": "lichess", "name": "KnightlyMe"}], time_classes=["rapid"])
        self.assertEqual(extract.games_of(c, self.c), [])  # the game is blitz


class ExportTest(unittest.TestCase):
    def test_every_account_gets_a_file(self):
        offline(self)
        d = tmpdir(self)
        c = cache_in(self, d)
        (d / "g.pgn").write_text(PGN_TEXT)
        old = game_from(FRENCH, white=True, id="old", end_ts=1000, black="Rival")
        c.put_games([scholar_game(), old])
        config = cfg(
            [
                {"source": "lichess", "name": "KnightlyMe"},
                {"source": "lichess", "name": "Rival", "role": OTHER},
                {"source": "pgn", "name": "Carlsen, Magnus", "role": OTHER},
            ],
            pgn_paths=[str(d / "g.pgn")],
            export_dir=str(d / "out"),
        )
        got = extract.run(config, c)
        self.assertEqual(
            got.exported,
            {"lichess-KnightlyMe.pgn": 2, "lichess-Rival.pgn": 1, "pgn-Carlsen_Magnus.pgn": 1, "pgn-inputs.pgn": 2},
        )
        with open(d / "out" / "lichess-KnightlyMe.pgn") as f:
            first = chess.pgn.read_game(f)
            second = chess.pgn.read_game(f)
        self.assertEqual(first.headers["Date"], "1970.01.01")  # oldest first
        self.assertEqual(second.headers["Site"], scholar_game().url)
        self.assertEqual(len(list(second.mainline_moves())), len(scholar_game().moves))
        self.assertFalse(any(p.suffix == ".tmp" for p in (d / "out").iterdir()))
        # Nothing new and the files are there: not written again.
        os.utime(d / "out" / "pgn-inputs.pgn", (1, 1))
        self.assertEqual(extract.run(config, c).exported, {})
        self.assertEqual((d / "out" / "pgn-inputs.pgn").stat().st_mtime, 1)
        # An account added later gets its file without waiting for new games.
        config.extract.accounts.append(Account("chesscom", "Newcomer", OTHER))
        self.assertEqual(extract.run(config, c).exported["chesscom-Newcomer.pgn"], 0)

    def test_game_pgn_round_trips(self):
        g = scholar_game()
        back = chess.pgn.read_game(io.StringIO(game_pgn(g)))
        self.assertEqual([m.uci() for m in back.mainline_moves()], g.moves)
        self.assertEqual(
            (back.headers["White"], back.headers["Black"], back.headers["Result"]), ("Opponent", "KnightlyMe", "1-0")
        )


if __name__ == "__main__":
    unittest.main()

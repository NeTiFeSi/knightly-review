"""Persistent cache for games, evals, HTTP validators and small state.

SQLite in WAL mode: one file, no server, safe to use from several threads (one
connection per thread) and several processes at once (the add-on and, later, the
companion). `Cache` is the interface; a Valkey/Redis version can implement it for
a hosted deployment.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from .model import Eval, Game

SCHEMA = """
CREATE TABLE IF NOT EXISTS evals (
  fen_key TEXT PRIMARY KEY,
  effort INTEGER NOT NULL,   -- the Stockfish depth the position was searched to (one line per legal move)
  data TEXT NOT NULL,
  updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
  fen_key TEXT NOT NULL,
  move TEXT NOT NULL,        -- the UCI move searched on its own; '' = the best line (one line); '*N' = the top N lines
  effort INTEGER NOT NULL,   -- the depth of the scan
  data TEXT NOT NULL,        -- an Eval: one line, or N for '*N'
  updated INTEGER NOT NULL,
  PRIMARY KEY (fen_key, move)
);
CREATE TABLE IF NOT EXISTS games (
  source TEXT NOT NULL,
  id TEXT NOT NULL,
  user TEXT NOT NULL,
  end_ts INTEGER NOT NULL,
  data TEXT NOT NULL,
  analysis TEXT,
  PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS games_user ON games (source, user, end_ts);
CREATE TABLE IF NOT EXISTS game_players (
  source TEXT NOT NULL,
  id TEXT NOT NULL,
  name TEXT NOT NULL,        -- lowercase, as the game names the player
  white INTEGER NOT NULL,    -- 1: played White
  PRIMARY KEY (source, id, white)
);
CREATE INDEX IF NOT EXISTS game_players_name ON game_players (name, source);
CREATE TABLE IF NOT EXISTS http (
  url TEXT PRIMARY KEY,
  etag TEXT,
  last_modified TEXT,
  fetched INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


SCHEMA_VERSION = 5  # 2: evals carry WDL; 3: evals per engine; 4: Stockfish only, every move searched together; 5: scans


def fen_key(fen: str) -> str:
    """FEN without the move counters, so transpositions share cache entries."""
    return " ".join(fen.split()[:4])


def _index_players(c: sqlite3.Connection, source: str, gid: str, white: str, black: str) -> None:
    for name, is_white in ((white, 1), (black, 0)):
        if name:
            c.execute(
                "INSERT OR IGNORE INTO game_players (source, id, name, white) VALUES (?,?,?,?)",
                (source, gid, name.lower(), is_white),
            )


class Cache(Protocol):
    def get_eval(self, key: str, min_effort: int = 0) -> Eval | None: ...
    def put_eval(self, key: str, effort: int, ev: Eval) -> None: ...
    def get_scan(self, key: str, names: Iterable[str], min_effort: int) -> dict[str, Eval]: ...
    def put_scan(self, key: str, effort: int, found: dict[str, Eval]) -> None: ...
    def put_games(self, games: Iterable[Game], user: str = "") -> int: ...
    def games(self, source: str, user: str, since_ts: int = 0) -> list[Game]: ...
    def games_from(self, source: str, since_ts: int = 0) -> list[Game]: ...
    def get_analysis(self, game: Game) -> list[dict | None] | None: ...
    def put_analysis(self, game: Game, analysis: list[dict | None]) -> None: ...
    def get_http(self, url: str) -> tuple[str | None, str | None]: ...
    def put_http(self, url: str, etag: str | None, last_modified: str | None) -> None: ...
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    def claim(self, key: str, owner: str, seconds: float) -> bool: ...
    def unclaim(self, key: str, owner: str) -> None: ...


class SqliteCache:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = c.execute("SELECT value FROM kv WHERE key='schema'").fetchone()
            old = not row or int(row[0]) < SCHEMA_VERSION
            if old:
                c.execute("DROP TABLE IF EXISTS evals")
                c.execute("DROP TABLE IF EXISTS scans")
            c.executescript(SCHEMA)
            if old:
                c.execute("UPDATE games SET analysis=NULL")
                c.execute("DELETE FROM kv WHERE key LIKE 'cloudmiss:%'")
                c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            if not c.execute("SELECT 1 FROM kv WHERE key='players_indexed'").fetchone():
                # Caches from before game_players: index the players of the games already stored.
                for source, gid, data in c.execute("SELECT source, id, data FROM games").fetchall():
                    d = json.loads(data)
                    _index_players(c, source, gid, d.get("white", ""), d.get("black", ""))
                c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('players_indexed', '1')")

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # evals
    def get_eval(self, key: str, min_effort: int = 0) -> Eval | None:
        """The position's search, if it went at least to depth `min_effort`."""
        row = self._conn().execute("SELECT data FROM evals WHERE fen_key=? AND effort>=?", (key, min_effort)).fetchone()
        return Eval.from_json(json.loads(row[0])) if row else None

    def put_eval(self, key: str, effort: int, ev: Eval) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO evals (fen_key, effort, data, updated) VALUES (?,?,?,?)
                   ON CONFLICT (fen_key) DO UPDATE SET
                     effort=excluded.effort, data=excluded.data, updated=excluded.updated
                   WHERE excluded.effort >= evals.effort""",
                (key, effort, json.dumps(ev.to_json()), int(time.time())),
            )

    # scans: the cheap first look at a position (see engine/analyse.py)
    def get_scan(self, key: str, names: Iterable[str], min_effort: int) -> dict[str, Eval]:
        """The saved scans called `names` (a UCI move, '' for the best line, '*N' for the top N lines), those
        searched at least to `min_effort`."""
        want = list(names)
        if not want:
            return {}
        rows = self._conn().execute(
            f"SELECT move, data FROM scans WHERE fen_key=? AND effort>=? AND move IN ({','.join('?' * len(want))})",
            (key, min_effort, *want),
        )
        return {m: Eval.from_json(json.loads(d)) for m, d in rows}

    def put_scan(self, key: str, effort: int, found: dict[str, Eval]) -> None:
        with self._conn() as c:
            for move, ev in found.items():
                c.execute(
                    """INSERT INTO scans (fen_key, move, effort, data, updated) VALUES (?,?,?,?,?)
                       ON CONFLICT (fen_key, move) DO UPDATE SET
                         effort=excluded.effort, data=excluded.data, updated=excluded.updated
                       WHERE excluded.effort >= scans.effort""",
                    (key, move, effort, json.dumps(ev.to_json()), int(time.time())),
                )

    # games
    def put_games(self, games: Iterable[Game], user: str = "") -> int:
        """Store new games (one already stored is left as it is). `user`: the account they were fetched for;
        `games` finds each game by either player's name anyway."""
        n = 0
        with self._conn() as c:
            for g in games:
                cur = c.execute(
                    "INSERT OR IGNORE INTO games (source, id, user, end_ts, data) VALUES (?,?,?,?,?)",
                    (g.source, g.id, user.lower(), g.end_ts, json.dumps(g.to_json())),
                )
                n += cur.rowcount
                _index_players(c, g.source, g.id, g.white, g.black)
        return n

    def games(self, source: str, user: str, since_ts: int = 0) -> list[Game]:
        """The games of `source` that `user` played (either colour), newest first."""
        rows = (
            self._conn()
            .execute(
                """SELECT g.data FROM games g JOIN game_players p ON p.source = g.source AND p.id = g.id
                   WHERE p.name=? AND g.source=? AND g.end_ts>=? ORDER BY g.end_ts DESC""",
                (user.lower(), source, since_ts),
            )
            .fetchall()
        )
        return [Game.from_json(json.loads(r[0])) for r in rows]

    def games_from(self, source: str, since_ts: int = 0) -> list[Game]:
        """Every game of `source`, newest first."""
        rows = (
            self._conn()
            .execute("SELECT data FROM games WHERE source=? AND end_ts>=? ORDER BY end_ts DESC", (source, since_ts))
            .fetchall()
        )
        return [Game.from_json(json.loads(r[0])) for r in rows]

    def get_analysis(self, game: Game) -> list[dict | None] | None:
        row = (
            self._conn()
            .execute("SELECT analysis FROM games WHERE source=? AND id=?", (game.source, game.id))
            .fetchone()
        )
        return json.loads(row[0]) if row and row[0] else None

    def put_analysis(self, game: Game, analysis: list[dict | None]) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE games SET analysis=? WHERE source=? AND id=?",
                (json.dumps(analysis), game.source, game.id),
            )

    # HTTP validators
    def get_http(self, url: str) -> tuple[str | None, str | None]:
        row = self._conn().execute("SELECT etag, last_modified FROM http WHERE url=?", (url,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def put_http(self, url: str, etag: str | None, last_modified: str | None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO http (url, etag, last_modified, fetched) VALUES (?,?,?,?)",
                (url, etag, last_modified, int(time.time())),
            )

    # small JSON state (sync cursors, cooldowns)
    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, json.dumps(value)))

    # leases: one owner at a time across threads and processes (engine/broker.py)
    def claim(self, key: str, owner: str, seconds: float) -> bool:
        """Hold `key` for `seconds` if it is free, expired or held by `owner` already (renewed). One statement,
        so two processes can't both win."""
        now = time.time()
        value = json.dumps({"owner": owner, "until": now + seconds})
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value
                   WHERE json_extract(kv.value, '$.owner')=? OR json_extract(kv.value, '$.until')<?""",
                (key, value, owner, now),
            )
            return cur.rowcount > 0

    def unclaim(self, key: str, owner: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM kv WHERE key=? AND json_extract(value, '$.owner')=?", (key, owner))

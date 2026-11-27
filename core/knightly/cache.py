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
  fen_key TEXT NOT NULL,
  engine TEXT NOT NULL,      -- "stockfish" | "lc0"
  multipv INTEGER NOT NULL,
  effort INTEGER NOT NULL,   -- Stockfish: depth; Lc0: milliseconds
  data TEXT NOT NULL,
  updated INTEGER NOT NULL,
  PRIMARY KEY (fen_key, engine, multipv)
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


SCHEMA_VERSION = 3  # 2: evals carry WDL; 3: evals are kept per engine (Stockfish, Lc0)


def fen_key(fen: str) -> str:
    """FEN without the move counters, so transpositions share cache entries."""
    return " ".join(fen.split()[:4])


class Cache(Protocol):
    def get_eval(self, key: str, engine: str, multipv: int = 1, min_effort: int = 0) -> Eval | None: ...
    def put_eval(self, key: str, engine: str, effort: int, ev: Eval) -> None: ...
    def put_games(self, games: Iterable[Game], user: str) -> int: ...
    def games(self, source: str, user: str, since_ts: int = 0) -> list[Game]: ...
    def best_evals(self, keys: list[str], engine: str) -> dict[str, tuple[int, Eval]]: ...
    def get_analysis(self, game: Game) -> list[dict | None] | None: ...
    def put_analysis(self, game: Game, analysis: list[dict | None]) -> None: ...
    def get_http(self, url: str) -> tuple[str | None, str | None]: ...
    def put_http(self, url: str, etag: str | None, last_modified: str | None) -> None: ...
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...


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
            c.executescript(SCHEMA)
            if old:
                c.execute("UPDATE games SET analysis=NULL")
                c.execute("DELETE FROM kv WHERE key LIKE 'cloudmiss:%'")
                c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

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
    def get_eval(self, key: str, engine: str, multipv: int = 1, min_effort: int = 0) -> Eval | None:
        row = (
            self._conn()
            .execute(
                "SELECT data FROM evals WHERE fen_key=? AND engine=? AND multipv>=? AND effort>=? "
                "ORDER BY effort DESC LIMIT 1",
                (key, engine, multipv, min_effort),
            )
            .fetchone()
        )
        if not row:
            return None
        ev = Eval.from_json(json.loads(row[0]))
        ev.lines = ev.lines[: max(multipv, 1)] if multipv > 0 else ev.lines
        return ev

    def best_evals(self, keys: list[str], engine: str) -> dict[str, tuple[int, Eval]]:
        """The most searched eval of each position (any number of lines): key → (effort, eval)."""
        best: dict[str, tuple[int, str]] = {}
        conn = self._conn()
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            rows = conn.execute(
                f"SELECT fen_key, effort, data FROM evals WHERE engine=? AND fen_key IN ({','.join('?' * len(chunk))})",
                (engine, *chunk),
            )
            for k, effort, data in rows:
                if k not in best or effort > best[k][0]:
                    best[k] = (effort, data)
        return {k: (e, Eval.from_json(json.loads(d))) for k, (e, d) in best.items()}

    def put_eval(self, key: str, engine: str, effort: int, ev: Eval) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO evals (fen_key, engine, multipv, effort, data, updated) VALUES (?,?,?,?,?,?)
                   ON CONFLICT (fen_key, engine, multipv) DO UPDATE SET
                     effort=excluded.effort, data=excluded.data, updated=excluded.updated
                   WHERE excluded.effort >= evals.effort""",
                (key, engine, len(ev.lines), effort, json.dumps(ev.to_json()), int(time.time())),
            )

    # games
    def put_games(self, games: Iterable[Game], user: str) -> int:
        n = 0
        with self._conn() as c:
            for g in games:
                cur = c.execute(
                    "INSERT OR IGNORE INTO games (source, id, user, end_ts, data) VALUES (?,?,?,?,?)",
                    (g.source, g.id, user.lower(), g.end_ts, json.dumps(g.to_json())),
                )
                n += cur.rowcount
        return n

    def games(self, source: str, user: str, since_ts: int = 0) -> list[Game]:
        rows = (
            self._conn()
            .execute(
                "SELECT data FROM games WHERE source=? AND user=? AND end_ts>=? ORDER BY end_ts DESC",
                (source, user.lower(), since_ts),
            )
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

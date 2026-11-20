"""Chess.com published-data API: monthly game archives.

Chess.com serves these from a cache that refreshes a few times a day, asks for
serial requests only, and supports ETag/Last-Modified. Past months never change,
so each is downloaded once; the current month is re-checked with a conditional
request.
"""

from __future__ import annotations

import io
import threading
import time
from typing import Callable, Optional

import chess.pgn

from .. import http
from ..model import Game

API = "https://api.chess.com/pub"
TIME_CLASS = {"daily": "correspondence"}


def parse_game(d: dict, user: str) -> Optional[Game]:
    if d.get("rules", "chess") != "chess":
        return None
    if d.get("initial_setup") and d["initial_setup"] != chess.STARTING_FEN:
        return None
    white = d.get("white", {}).get("username", "")
    black = d.get("black", {}).get("username", "")
    if white.lower() == user.lower():
        user_is_white = True
    elif black.lower() == user.lower():
        user_is_white = False
    else:
        return None
    pgn = chess.pgn.read_game(io.StringIO(d.get("pgn", "")))
    if pgn is None or pgn.errors:
        return None
    moves = [m.uci() for m in pgn.mainline_moves()]
    url = d.get("url", "")
    tc = d.get("time_class", "")
    return Game(
        source="chesscom",
        id=url.rstrip("/").rsplit("/", 1)[-1] or d.get("uuid", ""),
        url=url,
        white=white,
        black=black,
        user_is_white=user_is_white,
        end_ts=int(d.get("end_time", 0)),
        result=pgn.headers.get("Result", "*"),
        moves=moves,
        time_class=TIME_CLASS.get(tc, tc),
        rated=bool(d.get("rated")),
        white_elo=str(d.get("white", {}).get("rating", "")),
        black_elo=str(d.get("black", {}).get("rating", "")),
        event=f"{'Rated' if d.get('rated') else 'Casual'} {tc} game",
    )


def _month(url: str) -> tuple[int, int]:
    y, m = url.rstrip("/").split("/")[-2:]
    return int(y), int(m)


def fetch(
    cache,
    user: str,
    *,
    history_days: int = 90,
    time_classes: Optional[list[str]] = None,
    rated_only: bool = False,
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[str], None]] = None,
    session=None,
    now: Optional[float] = None,
) -> int:
    s = session or http.session()
    u = user.lower()
    r = http.get(s, f"{API}/player/{u}/games/archives", cancel=cancel)
    if r.status_code == 404:
        raise ValueError(f"Chess.com user {user!r} not found")
    r.raise_for_status()
    first = _first_month(history_days, now)
    done_key = f"chesscom_done:{u}"
    # Before the look-back was recorded, the oldest month was stored only in part: redo all once.
    done = set(cache.get(done_key, [])) if cache.get(_first_key(u)) is not None else set()
    cur = time.gmtime(now or time.time())
    current = (cur.tm_year, cur.tm_mon)
    archives = [a for a in r.json().get("archives", []) if _month(a) >= first and a not in done]
    new = 0
    for url in archives:
        if cancel is not None and cancel.is_set():
            raise http.Cancelled()
        etag, modified = cache.get_http(url)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        if progress:
            progress(f"Chess.com: {url.rsplit('/', 2)[-2]}/{url.rsplit('/', 1)[-1]}…")
        resp = http.get(s, url, headers=headers, cancel=cancel)
        if resp.status_code == 304:
            continue
        resp.raise_for_status()
        games = []
        for d in resp.json().get("games", []):
            # Every game of the month is kept, whatever the look-back: the month is then
            # really done, even if the look-back is extended later (the update filters by date).
            g = parse_game(d, user)
            if not g:
                continue
            if time_classes and g.time_class not in time_classes:
                continue
            if rated_only and not g.rated:
                continue
            games.append(g)
        new += cache.put_games(games, user)
        cache.put_http(url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
        if _month(url) < current:
            done.add(url)  # a finished month never changes
        cache.set(done_key, sorted(done))  # after each month, so a cancelled backfill resumes
    cache.set(_first_key(u), list(first))
    return new


def _first_month(history_days: int, now: Optional[float] = None) -> tuple[int, int]:
    if history_days <= 0:
        return (0, 0)  # all time
    t = time.gmtime((now or time.time()) - history_days * 86400)
    return (t.tm_year, t.tm_mon)


def _first_key(user: str) -> str:
    return f"chesscom_first:{user.lower()}"


def needs_backfill(cache, user: str, history_days: int, now: Optional[float] = None) -> bool:
    """True when the look-back now reaches further back than any fetch so far."""
    got = cache.get(_first_key(user))
    return got is None or _first_month(history_days, now) < tuple(got)

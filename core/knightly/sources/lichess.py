"""Lichess games export (NDJSON) and account lookup.

Only a handful of endpoints, so a small `requests` client instead of berserk.
Lichess asks clients to make one request at a time and to wait a full minute
after HTTP 429; `http.get` does that.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

import chess

from .. import http
from ..model import Game

API = "https://lichess.org"
SPEEDS = {"ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence"}


def parse_game(d: dict, user: str) -> Game | None:
    """One NDJSON record → Game, or None for variants, imported positions or games without the user."""
    if d.get("variant", "standard") != "standard" or d.get("initialFen"):
        return None
    players = d.get("players", {})
    names = {c: (players.get(c, {}).get("user") or {}).get("name", "") for c in ("white", "black")}
    if names["white"].lower() == user.lower():
        user_is_white = True
    elif names["black"].lower() == user.lower():
        user_is_white = False
    else:
        return None
    board = chess.Board()
    moves = []
    try:
        for san in d.get("moves", "").split():
            mv = board.parse_san(san)
            moves.append(mv.uci())
            board.push(mv)
    except ValueError:
        return None
    winner = d.get("winner")
    if winner:
        result = "1-0" if winner == "white" else "0-1"
    elif d.get("status") in ("started", "created"):
        result = "*"
    else:
        result = "1/2-1/2"
    speed = d.get("speed", "")
    return Game(
        source="lichess",
        id=d["id"],
        url=f"{API}/{d['id']}" + ("" if user_is_white else "/black"),
        white=names["white"],
        black=names["black"],
        user_is_white=user_is_white,
        end_ts=int(d.get("lastMoveAt", d.get("createdAt", 0)) / 1000),
        result=result,
        moves=moves,
        time_class=speed,
        rated=bool(d.get("rated")),
        white_elo=str(players.get("white", {}).get("rating", "")),
        black_elo=str(players.get("black", {}).get("rating", "")),
        event=f"{'Rated' if d.get('rated') else 'Casual'} {speed} game",
    )


def _keys(user: str) -> tuple[str, str]:
    """Cache keys: the cursor (createdAt of the newest stored game + 1, in ms) and the floor
    (where the stored, gap-free run of games starts: 0 = all time)."""
    return f"cursor:lichess:{user.lower()}", f"floor:lichess:{user.lower()}"


def _start_ms(history_days: int) -> int:
    return 0 if history_days <= 0 else int((time.time() - history_days * 86400) * 1000)


def needs_backfill(cache, user: str, history_days: int) -> bool:
    """True when the look-back now reaches further back than the games fetched so far."""
    floor = cache.get(_keys(user)[1])
    return floor is None or _start_ms(history_days) < int(floor)


def fetch(
    cache,
    user: str,
    *,
    token: str = "",
    history_days: int = 90,
    time_classes: list[str] | None = None,
    rated_only: bool = False,
    max_games: int | None = None,
    cancel: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
    session=None,
) -> int:
    """Download the user's games newer than the last fetch into the cache. Returns how many are new.

    `history_days` 0 or negative fetches the whole history. When the look-back grows, the next
    fetch starts over from the new start date (games already stored are skipped). Games are
    saved as they stream in, so a cancelled or failed download resumes where it stopped.
    """
    cursor_key, floor_key = _keys(user)
    start = _start_ms(history_days)
    floor = cache.get(floor_key)
    cursor = int(cache.get(cursor_key, 0))
    if floor is None or start < int(floor) or not cursor:
        cursor = start
        cache.set(cursor_key, cursor)
        cache.set(floor_key, start)
    params = {
        "since": cursor,
        "moves": "true",
        "finished": "true",
        "sort": "dateAsc",
    }
    if max_games:
        params["max"] = max_games
    perfs = [t for t in (time_classes or []) if t in SPEEDS]
    if perfs:
        params["perfType"] = ",".join(perfs)
    if rated_only:
        params["rated"] = "true"
    s = session or http.session(token)
    r = http.get(
        s,
        f"{API}/api/games/user/{user}",
        params=params,
        headers={"Accept": "application/x-ndjson"},
        stream=True,
        cancel=cancel,
        on_wait=(lambda w: progress and progress(f"Lichess asked us to wait {w:.0f} s…")),
    )
    if r.status_code == 404:
        raise ValueError(f"Lichess user {user!r} not found")
    r.raise_for_status()
    batch: list[Game] = []
    newest, seen, n = cursor, 0, 0

    def save() -> int:
        k = cache.put_games(batch, user)
        cache.set(cursor_key, newest)  # dateAsc: everything before `newest` is stored
        batch.clear()
        return k

    with r:
        for line in r.iter_lines():
            if cancel is not None and cancel.is_set():
                break
            if not line:
                continue
            d = json.loads(line)
            newest = max(newest, int(d.get("createdAt", 0)) + 1)
            seen += 1
            g = parse_game(d, user)
            if g:
                batch.append(g)
            if seen % 100 == 0:
                n += save()
                if progress:
                    day = time.strftime("%Y-%m-%d", time.gmtime(newest / 1000))
                    progress(f"Lichess: {seen} games so far (up to {day})…")
    n += save()
    return n


def account(token: str, *, session=None) -> dict:
    s = session or http.session(token)
    r = http.get(s, f"{API}/api/account")
    r.raise_for_status()
    return r.json()

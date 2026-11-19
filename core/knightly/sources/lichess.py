"""Lichess games export (NDJSON) and account lookup.

Only a handful of endpoints, so a small `requests` client instead of berserk.
Lichess asks clients to make one request at a time and to wait a full minute
after HTTP 429; `http.get` does that.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import chess

from .. import http
from ..model import Game

API = "https://lichess.org"
SPEEDS = {"ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence"}


def parse_game(d: dict, user: str) -> Optional[Game]:
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


def fetch(
    cache,
    user: str,
    *,
    token: str = "",
    history_days: int = 90,
    time_classes: Optional[list[str]] = None,
    rated_only: bool = False,
    max_games: int = 300,
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[str], None]] = None,
    session=None,
) -> int:
    """Download the user's games newer than the last fetch into the cache. Returns how many are new."""
    cursor_key = f"cursor:lichess:{user.lower()}"
    since_ms = int(cache.get(cursor_key, 0)) or int((time.time() - history_days * 86400) * 1000)
    params = {
        "since": since_ms,
        "max": max_games,
        "moves": "true",
        "finished": "true",
        "sort": "dateAsc",
    }
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
    games, newest = [], since_ms
    with r:
        for line in r.iter_lines():
            if cancel is not None and cancel.is_set():
                break
            if not line:
                continue
            d = json.loads(line)
            newest = max(newest, int(d.get("createdAt", 0)) + 1)
            g = parse_game(d, user)
            if g:
                games.append(g)
                if progress and len(games) % 10 == 0:
                    progress(f"Lichess: {len(games)} games…")
    n = cache.put_games(games, user)
    cache.set(cursor_key, newest)
    return n


def account(token: str, *, session=None) -> dict:
    s = session or http.session(token)
    r = http.get(s, f"{API}/api/account")
    r.raise_for_status()
    return r.json()

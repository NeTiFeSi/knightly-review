"""Game sources. Each `fetch()` stores new games in the cache and honours the site's limits."""

from __future__ import annotations

import time

# Shortest gap between two automatic fetches from one site. Manual updates skip
# this for Lichess (incremental and cheap), but Chess.com answers from a cache
# that changes only a few times a day, so asking more often gains nothing.
MIN_INTERVAL = {"lichess": 30 * 60, "chesscom": 3 * 3600}


def cooldown_key(source: str, user: str) -> str:
    return f"next_fetch:{source}:{user.lower()}"


def due(cache, source: str, user: str, now: float | None = None) -> bool:
    return (now or time.time()) >= float(cache.get(cooldown_key(source, user), 0))


def mark_fetched(cache, source: str, user: str, now: float | None = None) -> None:
    cache.set(cooldown_key(source, user), (now or time.time()) + MIN_INTERVAL[source])

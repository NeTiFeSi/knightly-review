"""Game sources. Each `fetch()` stores new games in the cache and honours the site's limits."""

from __future__ import annotations

import time

# Shortest gap between two automatic fetches from one site. Manual updates skip
# this for Lichess (incremental and cheap), but Chess.com answers from a cache
# that changes only a few times a day, so asking more often gains nothing.
MIN_INTERVAL = {"lichess": 30 * 60, "chesscom": 3 * 3600}


def since_ts(history_days: int, now: float | None = None) -> int:
    """Oldest game end time to keep. `history_days` 0 or negative means all time."""
    if history_days <= 0:
        return 0
    return int((now or time.time()) - history_days * 86400)


def cooldown_key(source: str, user: str) -> str:
    return f"next_fetch:{source}:{user.lower()}"


def due(cache, source: str, user: str, now: float | None = None) -> bool:
    return (now or time.time()) >= float(cache.get(cooldown_key(source, user), 0))


def mark_fetched(cache, source: str, user: str, now: float | None = None) -> None:
    cache.set(cooldown_key(source, user), (now or time.time()) + MIN_INTERVAL[source])


# The game filters are applied when downloading, so games they left out are never stored. When they let more
# games through than at the last download, the look-back is downloaded again (games already stored are skipped).


def _filters_key(source: str, user: str) -> str:
    return f"fetch_filters:{source}:{user.lower()}"


def _filters(time_classes: list[str] | None, rated_only: bool) -> dict:
    return {"time_classes": sorted(time_classes) if time_classes else None, "rated_only": bool(rated_only)}


def filters_widened(cache, source: str, user: str, time_classes: list[str] | None, rated_only: bool) -> bool:
    """True when these filters let through games the ones of the last download left out. Unknown (nothing
    recorded yet) counts as unchanged."""
    old = cache.get(_filters_key(source, user))
    if not old:
        return False
    new = _filters(time_classes, rated_only)
    more_classes = old["time_classes"] is not None and (
        new["time_classes"] is None or bool(set(new["time_classes"]) - set(old["time_classes"]))
    )
    return more_classes or (old["rated_only"] and not new["rated_only"])


def mark_filters(cache, source: str, user: str, time_classes: list[str] | None, rated_only: bool) -> None:
    cache.set(_filters_key(source, user), _filters(time_classes, rated_only))

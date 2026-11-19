"""Polite HTTP: one shared session, a project User-Agent, and waiting on HTTP 429."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

import requests

from . import USER_AGENT


class Cancelled(Exception):
    pass


class RateLimited(Exception):
    """The site asked us to slow down and retries ran out."""


def session(token: str = "") -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def sleep(seconds: float, cancel: Optional[threading.Event]) -> None:
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise Cancelled()


def get(
    s: requests.Session,
    url: str,
    *,
    cancel: Optional[threading.Event] = None,
    retries: int = 2,
    wait_429: float = 60.0,
    on_wait: Optional[Callable[[float], None]] = None,
    **kw: Any,
) -> requests.Response:
    """GET that waits `wait_429` seconds (or Retry-After) on HTTP 429, then retries."""
    kw.setdefault("timeout", 30)
    for attempt in range(retries + 1):
        r = s.get(url, **kw)
        if r.status_code != 429:
            return r
        r.close()
        if attempt == retries:
            break
        try:
            wait = float(r.headers.get("Retry-After", wait_429))
        except ValueError:
            wait = wait_429
        if on_wait:
            on_wait(wait)
        sleep(wait, cancel)
    raise RateLimited(url)

"""Polite HTTP: one shared session, a project User-Agent, and waiting on HTTP 429."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from . import USER_AGENT


class Cancelled(Exception):
    pass


class RateLimited(Exception):
    """The site asked us to slow down and retries ran out."""


def session(token: str = "", contact: str = "") -> requests.Session:
    """`contact`: an email address added to the User-Agent, so the site can reach whoever runs this
    (Chess.com's API asks for one)."""
    s = requests.Session()
    s.headers["User-Agent"] = f"{USER_AGENT} contact: {contact}" if contact else USER_AGENT
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def sleep(seconds: float, cancel: threading.Event | None) -> None:
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise Cancelled()


def get(
    s: requests.Session,
    url: str,
    *,
    cancel: threading.Event | None = None,
    retries: int = 2,
    wait_429: float = 60.0,
    on_wait: Callable[[float], None] | None = None,
    **kw: Any,
) -> requests.Response:
    """GET that waits `wait_429` seconds (or Retry-After) on HTTP 429, then retries."""
    return _request(s.get, url, cancel=cancel, retries=retries, wait_429=wait_429, on_wait=on_wait, **kw)


def post(
    s: requests.Session,
    url: str,
    *,
    cancel: threading.Event | None = None,
    retries: int = 2,
    wait_429: float = 60.0,
    on_wait: Callable[[float], None] | None = None,
    **kw: Any,
) -> requests.Response:
    """POST, waiting on HTTP 429 like `get`."""
    return _request(s.post, url, cancel=cancel, retries=retries, wait_429=wait_429, on_wait=on_wait, **kw)


def _request(
    send: Callable[..., requests.Response],
    url: str,
    *,
    cancel: threading.Event | None,
    retries: int,
    wait_429: float,
    on_wait: Callable[[float], None] | None,
    **kw: Any,
) -> requests.Response:
    kw.setdefault("timeout", 30)
    for attempt in range(retries + 1):
        r = send(url, **kw)
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

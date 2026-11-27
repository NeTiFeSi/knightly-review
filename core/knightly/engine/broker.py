"""The shared Stockfish: a pool of engine processes handed out by priority.

Every stage that needs the engine (annotating, confirming candidates, building puzzles, …) holds an `Engine`
(analyse.py) that answers from the cache when it can and asks the pool for a process only to search. When
several ask at once, the most urgent gets the next free process (`INTERACTIVE` before `CONFIRM`, then `BUILD`,
`SCAN`, `BACKFILL`; first come first served within a priority). A search is never interrupted to make room:
priorities order the waiting, not the running.

Across processes of the app (the add-on and a companion, or two Anki profiles) a lease in the cache lets only
one of them run Stockfish at a time; the others wait for it, or for it to expire if its holder died.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import ClassVar

import chess.engine

from ..config import Config
from ..http import Cancelled
from .discover import is_stockfish, popen_kwargs

log = logging.getLogger(__name__)

INTERACTIVE = 0  # someone is waiting for the answer (a card, a dialog)
CONFIRM = 10  # the full search that confirms a puzzle candidate
BUILD = 20  # the searches that build a puzzle's solution tree
SCAN = 30  # the cheap scan of every position (annotation)
BACKFILL = 40  # work for later (pre-calculating)

WDL_OPTION = "UCI_ShowWDL"
LEASE_KEY = "engine_lease"
LEASE_SECONDS = 120  # a lease not renewed for this long is free again (its holder died)


class EngineError(RuntimeError):
    pass


PROCESS = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"  # this process, as a lease owner


class Lease:
    """The right to run Stockfish, shared by every process using the same cache. The owner is the process: the
    pools of one process share its lease (they coordinate among themselves), and it is given back when the last
    of them lets go."""

    _holders: ClassVar[dict[str, int]] = {}  # cache path → leases of this process held on it
    _lock = threading.Lock()

    def __init__(self, cache, owner: str = PROCESS, seconds: float = LEASE_SECONDS, clock=time.time):
        self.cache, self.owner, self.seconds, self.clock = cache, owner, seconds, clock
        self._key = f"{getattr(cache, 'path', id(cache))}:{owner}"
        self._until = 0.0
        self._counted = False

    def acquire(self, cancel: threading.Event | None = None, poll: float = 1.0) -> None:
        """Take the lease (or renew it when half its time is gone), waiting while another process holds it."""
        if self.clock() < self._until - self.seconds / 2:
            return
        while not self.cache.claim(LEASE_KEY, self.owner, self.seconds):
            if cancel is not None and cancel.wait(poll):
                raise Cancelled()
            if cancel is None:
                time.sleep(poll)
        self._until = self.clock() + self.seconds
        if not self._counted:
            self._counted = True
            with self._lock:
                self._holders[self._key] = self._holders.get(self._key, 0) + 1

    def release(self) -> None:
        if not self._counted:
            return
        self._counted, self._until = False, 0.0
        with self._lock:
            left = self._holders.get(self._key, 1) - 1
            if left:
                self._holders[self._key] = left
            else:
                self._holders.pop(self._key, None)
        if not left:
            self.cache.unclaim(LEASE_KEY, self.owner)


class Pool:
    """Up to `size` Stockfish processes, started when first needed and lent out by priority."""

    def __init__(
        self,
        cfg: Config,
        *,
        size: int = 1,
        lease: Lease | None = None,
        start: Callable[[], chess.engine.SimpleEngine] | None = None,
    ):
        self.cfg, self.size, self.lease = cfg, max(1, size), lease
        self._start = start or self._popen
        self._idle: list[chess.engine.SimpleEngine] = []
        self._count = 0  # processes started and not closed
        self._waiting: list[tuple[int, int]] = []  # heap of (priority, ticket)
        self._tickets = itertools.count()
        self._cond = threading.Condition()
        self._closed = False
        self.name = ""
        self.started = 0  # processes started so far

    def _popen(self) -> chess.engine.SimpleEngine:
        path = self.cfg.engine.stockfish_path
        if not path:
            raise EngineError("no Stockfish configured")
        eng = chess.engine.SimpleEngine.popen_uci(path, **popen_kwargs())
        name = eng.id.get("name", "")
        if not is_stockfish(name):
            eng.quit()
            raise EngineError(f"{path} is {name or 'not a known engine'}, not Stockfish")
        if WDL_OPTION not in eng.options:
            eng.quit()
            raise EngineError(f"{name} doesn't report win/draw/loss ({WDL_OPTION}); please update it")
        conf = {WDL_OPTION: True, "Threads": self.cfg.engine.engine_threads(), "Hash": self.cfg.engine.hash_mb}
        eng.configure({k: v for k, v in conf.items() if k in eng.options})
        return eng

    def inject(self, proc) -> None:
        """Use `proc` as an idle process (tests)."""
        with self._cond:
            self._idle.append(proc)
            self._count += 1
            self.name = self.name or getattr(proc, "id", {}).get("name", "")

    @contextmanager
    def acquire(self, priority: int = CONFIRM, cancel: threading.Event | None = None) -> Iterator:
        """A process for one search: the next free one once every more urgent (or earlier) request is served."""
        ticket = (priority, next(self._tickets))
        with self._cond:
            heapq.heappush(self._waiting, ticket)
            try:
                while True:
                    if self._closed:
                        raise Cancelled()
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    if self._waiting[0] == ticket and (self._idle or self._count < self.size):
                        break
                    self._cond.wait(0.2)
            except BaseException:
                self._waiting.remove(ticket)
                heapq.heapify(self._waiting)
                self._cond.notify_all()
                raise
            heapq.heappop(self._waiting)
            proc = self._idle.pop() if self._idle else None
            if proc is None:
                self._count += 1  # reserved: started below, outside the lock
            self._cond.notify_all()
        try:
            if self.lease is not None:
                self.lease.acquire(cancel)
            if proc is None:
                proc = self._start()
                self.started += 1
                self.name = self.name or proc.id.get("name", "")
        except BaseException:
            with self._cond:
                if proc is None:
                    self._count -= 1
                else:
                    self._idle.append(proc)
                self._cond.notify_all()
            raise
        broken = False
        try:
            yield proc
        except (chess.engine.EngineError, chess.engine.EngineTerminatedError):
            broken = True
            raise
        finally:
            with self._cond:
                if broken or self._closed:
                    self._count -= 1
                    _quit(proc)
                else:
                    self._idle.append(proc)
                self._cond.notify_all()

    def close(self) -> None:
        """Stop the idle processes (busy ones stop when their search returns) and give the lease back."""
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._count -= len(idle)
            self._cond.notify_all()
        for p in idle:
            _quit(p)
        if self.lease is not None:
            self.lease.release()


def _quit(proc) -> None:
    try:
        proc.quit()
    except Exception:
        log.debug("engine did not quit cleanly", exc_info=True)

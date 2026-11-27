"""What a run is doing, for the progress window and the command line.

A `Tracker` is what the work reports to, much like a queue that counts what was put in (`plan`, the total)
and what was taken out and finished (`advance`, done):

- the **stage** (`stage`): a step with a message, a unit (moves, positions…) and a total that grows as work is
  planned, like a tqdm bar; each change is a *forced* update for the listeners, while `advance` is throttled;
- the **engine** (`search_start/progress/end`, fed by `engine.analyse.Engine`): the searches run, the
  ones answered from the cache, and the position being searched with its live depth;
- the **run**: puzzles found of wanted, the ply being worked on, the time budget.

`snapshot()` is an immutable copy that can be read from any thread (the window polls it); `describe()` turns one
into the texts and ratios a surface shows, so that formatting is testable without Qt.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

STOCKFISH = "stockfish"


@dataclass(frozen=True)
class EngineStats:
    name: str = ""
    enabled: bool = False
    searches: int = 0  # searches run
    cached: int = 0  # answered from the cache
    ms: int = 0  # time spent searching
    busy: bool = False
    fen: str = ""  # the position being searched
    ply: int = -1
    multipv: int = 0
    target: int = 0  # the depth being searched to
    depth: int = 0
    nodes: int = 0
    nps: int = 0
    elapsed_ms: int = 0


@dataclass(frozen=True)
class Snapshot:
    stage: str = "idle"  # fetch | analyse | cards | start | done | error | idle
    message: str = ""
    step: str = ""  # "depth 9 of 13"
    unit: str = ""  # what the stage counts
    done: int = 0
    total: int = 0
    rate: float = 0.0  # units per second
    eta: float | None = None  # seconds left for the stage, when known
    found: int = 0
    want: int = 0
    ply: int = -1
    max_ply: int = 0
    position: str = ""  # the position being worked on, in words
    minutes_left: float | None = None
    elapsed: float = 0.0
    engines: dict = field(default_factory=dict)  # kind → EngineStats


Listener = Callable[[Snapshot], None]


class Tracker:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, min_interval: float = 0.1):
        self._lock = threading.RLock()
        self._clock = clock
        self._min = min_interval
        self._t0 = clock()
        self._last_emit = -1e9
        self._listeners: list[Listener] = []
        self._s = Snapshot(engines={STOCKFISH: EngineStats()})
        self._samples: deque[tuple[float, int]] = deque(maxlen=30)

    # --- listeners
    def listen(self, fn: Listener) -> None:
        with self._lock:
            self._listeners.append(fn)

    def _emit(self, force: bool) -> None:
        with self._lock:
            now = self._clock()
            if not force and now - self._last_emit < self._min:
                return
            self._last_emit = now
            listeners = list(self._listeners)
        if listeners:
            snap = self.snapshot()
            for fn in listeners:
                fn(snap)

    # --- stage
    def stage(self, kind: str, message: str = "", *, total: int = 0, unit: str = "", step: str = "") -> None:
        """Start a step (forced update): `kind` is fetch, analyse, cards, start, done or error."""
        with self._lock:
            self._samples.clear()
            self._s = replace(
                self._s, stage=kind, message=message, step=step, unit=unit, done=0, total=total, rate=0.0, eta=None
            )
        self._emit(True)

    @contextmanager
    def sub(self, kind: str, message: str = "", **kw):
        """A short stage inside the current one (grading a game's moves): the outer stage comes back after it."""
        with self._lock:
            saved, samples = self._s, list(self._samples)
        self.stage(kind, message, **kw)
        try:
            yield
        finally:
            with self._lock:
                self._s = replace(
                    self._s,
                    stage=saved.stage,
                    message=saved.message,
                    step=saved.step,
                    unit=saved.unit,
                    done=saved.done,
                    total=saved.total,
                    rate=saved.rate,
                    eta=saved.eta,
                )
                self._samples = deque(samples, maxlen=30)
            self._emit(True)

    def message(self, message: str, *, step: str | None = None, force: bool = True) -> None:
        with self._lock:
            self._s = replace(self._s, message=message, step=self._s.step if step is None else step)
        self._emit(force)

    def plan(self, n: int = 1) -> None:
        """`n` more units of work will be done in this stage (the total grows)."""
        with self._lock:
            self._s = replace(self._s, total=self._s.total + n)
        self._emit(False)

    def advance(self, n: int = 1) -> None:
        with self._lock:
            now = self._clock()
            done = self._s.done + n
            self._samples.append((now, done))
            rate, eta = self._rate(done, self._s.total)
            self._s = replace(self._s, done=done, rate=rate, eta=eta)
        self._emit(False)

    def _rate(self, done: int, total: int) -> tuple[float, float | None]:
        if len(self._samples) < 2:
            return 0.0, None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        if t1 <= t0 or d1 <= d0:
            return 0.0, None
        rate = (d1 - d0) / (t1 - t0)
        return rate, ((total - done) / rate if total > done else 0.0)

    # --- the run
    def found(self, found: int, want: int) -> None:
        with self._lock:
            self._s = replace(self._s, found=found, want=want)
        self._emit(False)

    def plies(self, ply: int, max_ply: int) -> None:
        with self._lock:
            self._s = replace(self._s, ply=ply, max_ply=max_ply)

    def position(self, text: str) -> None:
        with self._lock:
            self._s = replace(self._s, position=text)

    def budget(self, minutes_left: float | None) -> None:
        with self._lock:
            self._s = replace(self._s, minutes_left=minutes_left)

    # --- engines
    def _engine(self, kind: str, **changes) -> None:
        with self._lock:
            eng = replace(self._s.engines.get(kind, EngineStats()), **changes)
            self._s = replace(self._s, engines={**self._s.engines, kind: eng})

    def engine(self, kind: str, name: str = "", enabled: bool = True) -> None:
        self._engine(kind, name=name, enabled=enabled)

    def search_start(self, kind: str, fen: str, ply: int, multipv: int, target: int) -> None:
        self._engine(
            kind, busy=True, fen=fen, ply=ply, multipv=multipv, target=target, depth=0, nodes=0, nps=0, elapsed_ms=0
        )
        self._emit(True)

    def search_progress(self, kind: str, depth: int = 0, nodes: int = 0, nps: int = 0, ms: int = 0) -> None:
        self._engine(kind, depth=depth, nodes=nodes, nps=nps, elapsed_ms=ms)

    def search_end(self, kind: str, cached: bool = False, ms: int = 0) -> None:
        with self._lock:
            e = self._s.engines.get(kind, EngineStats())
            self._engine(
                kind,
                busy=False,
                cached=e.cached + (1 if cached else 0),
                searches=e.searches + (0 if cached else 1),
                ms=e.ms + ms,
            )
        self._emit(False)

    # --- reading
    def snapshot(self) -> Snapshot:
        with self._lock:
            return replace(self._s, elapsed=self._clock() - self._t0, engines=dict(self._s.engines))


# ---------------------------------------------------------------- texts


def _clock_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def describe_engine(kind: str, e: EngineStats) -> dict:
    """One engine as the window shows it: a label, a ratio (0..1 or None) and the detail line."""
    label = e.name or "Stockfish"
    if e.busy and e.target:
        ratio = min(1.0, e.depth / e.target)
        where = f"ply {e.ply} · " if e.ply >= 0 else ""
        now = f"depth {e.depth} of {e.target}"
        lines = f" · {e.multipv} lines" if e.multipv > 1 else ""
        speed = f" · {e.nps / 1000:.0f} knps" if e.nps else ""
        detail = f"{where}{now}{lines}{speed}"
    else:
        ratio, detail = None, "idle"
    counts = f"{e.searches} searched · {e.cached} from the cache"
    return {"label": label, "ratio": ratio, "detail": detail, "counts": counts, "busy": e.busy, "enabled": e.enabled}


def describe(s: Snapshot) -> dict:
    """The texts and ratios of a snapshot."""
    unit = f" {s.unit}" if s.unit else ""
    stage = ""
    if s.total:
        stage = f"{s.done} of {s.total}{unit}"
        if s.rate:
            stage += f" · {s.rate:.1f}/s"
        if s.eta:
            stage += f" · {_clock_text(s.eta)} left"
    elif s.done:
        stage = f"{s.done}{unit}"
    return {
        "message": s.message,
        "step": s.step,
        "stage": stage,
        "stage_ratio": (s.done / s.total) if s.total else None,
        "found": f"{s.found} of {s.want} puzzles" if s.want else f"{s.found} puzzles",
        "found_ratio": (min(1.0, s.found / s.want) if s.want else None),
        "ply": (f"ply {s.ply}" + (f" of {s.max_ply}" if s.max_ply else "")) if s.ply >= 0 else "",
        "position": s.position,
        "budget": f"{s.minutes_left:.0f} min left" if s.minutes_left is not None else "",
        "elapsed": _clock_text(s.elapsed),
        "engines": {k: describe_engine(k, e) for k, e in s.engines.items() if e.enabled},
    }


def legacy_listener(progress: Callable[[str, int, int, str], None]) -> Listener:
    """Adapt snapshots to the older `progress(stage, done, total, message)` calls: the stage's message (with the
    puzzles found so far for the analysis stages) when it changes, and `("positions", done, total, step)`."""
    last: list = [None]

    def on(s: Snapshot) -> None:
        if s.stage == "idle":
            return
        key = (s.stage, s.message, s.found, s.want)
        if key != last[0] and s.stage in ("analyse", "cards"):
            last[0] = key
            progress(s.stage, s.found, s.want, s.message)
        elif key != last[0]:
            last[0] = key
            progress(s.stage, s.done, s.total, s.message)
        if s.total and s.stage in ("analyse", "cards"):
            progress("positions", s.done, s.total, s.step or s.message)

    return on

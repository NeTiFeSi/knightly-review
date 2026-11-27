"""Hosts the stages' supervisor (core/knightly/supervisor.py) in Anki, and writes what they make.

The stages run on threads of their own with no collection access, so reviewing stays responsive; they are
stopped when the profile closes. Two things need the collection, on the main thread:

- the notes already in it, which the stages ask for (`_existing`);
- the outbox, where the build and manage stages put the notes they make and the notes to suspend: a timer
  drains it every few seconds, each batch one undo step (kc.RunWriter). What is left there when the profile
  closes is written the next time it opens.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from aqt import gui_hooks, mw
from aqt.operations import CollectionOp
from aqt.qt import QTimer
from aqt.utils import tooltip

from . import collection as kc
from . import settings
from .vendor.knightly.cache import SqliteCache
from .vendor.knightly.engine.broker import Lease, Pool
from .vendor.knightly.http import Cancelled
from .vendor.knightly.model import NoteSpec
from .vendor.knightly.progress import Tracker, legacy_listener
from .vendor.knightly.supervisor import NOTE, STAGES, SUSPEND, Context, Outcome, StageState, Supervisor

log = logging.getLogger(__name__)

Listener = Callable[[str, int, int, str], None]
LABELS = {"extract": "Games", "manage": "Notes", "annotate": "Annotating", "select": "Candidates", "build": "Puzzles"}
DRAIN_MS = 3000
START_DELAY_MS = 20_000  # after the profile opens, so Anki starts at full speed


class Host:
    def __init__(self) -> None:
        self.sup: Supervisor | None = None
        self.cache: SqliteCache | None = None
        self.pool: Pool | None = None
        self.listeners: list[Listener] = []
        self.last: tuple[str, int, int, str] = ("idle", 0, 0, "Nothing has run yet.")
        self.positions: tuple[int, int, str] = (0, 0, "")
        # Everything the stages reported, for the progress window's log (also what happened before it was
        # opened). Entries are (sequence number, line) so a reader can pick up only the new ones.
        self.history: deque[tuple[int, str]] = deque(maxlen=1000)
        self._seq = 0
        self._logged = ""
        self._lock = threading.Lock()
        self.tracker = Tracker()  # the stage the progress window shows: the one that started last
        self._last_emit = 0.0
        self._pending = False
        self.drainer: QTimer | None = None
        self._draining = False
        self._wrote: kc.RunWriter | None = None
        self.finished: dict[str, float] = {}  # stage → when its last run ended (time.time())

    # --- what the progress window reads
    @property
    def running(self) -> bool:
        return self.sup is not None and self.sup.busy

    @property
    def keeping(self) -> bool:
        return self.sup is not None and self.sup.keep_until > time.monotonic()

    def log(self, line: str) -> None:
        with self._lock:
            self._seq += 1
            self.history.append((self._seq, time.strftime("%H:%M:%S  ") + line))

    def history_since(self, seq: int) -> list[tuple[int, str]]:
        with self._lock:
            return [h for h in self.history if h[0] > seq]

    # progress → listeners, always on the main thread, at most every 0.2 s; the last report of a
    # burst is still delivered.
    def _emit(self, stage: str, done: int, total: int, msg: str, force: bool = False) -> None:
        now = time.monotonic()
        if stage == "positions":
            self.positions = (done, total, msg)
        else:
            self.last = (stage, done, total, msg)
            if msg and msg != self._logged:
                self._logged = msg
                self.log(msg)
        if force or (now - self._last_emit >= 0.2 and not self._pending):
            self._last_emit = now
            mw.taskman.run_on_main(self._deliver)
        elif not self._pending:
            self._pending = True
            mw.taskman.run_on_main(lambda: QTimer.singleShot(200, self._deliver))

    def _deliver(self) -> None:
        self._pending = False
        self._last_emit = time.monotonic()
        for f in list(self.listeners):
            f(*self.last)

    # --- the supervisor
    def launch(self) -> bool:
        """Start the stages with the current settings (the fetch at start when the settings say so)."""
        if self.sup is not None or mw.col is None:
            return self.sup is not None
        cfg = settings.load()
        if not settings.configured(cfg):
            return False
        core = settings.core_config(cfg)
        self.cache = SqliteCache(settings.CACHE_PATH)
        self.pool = Pool(core, lease=Lease(self.cache))
        ctx = Context(core, self.cache, self.pool, threading.Event(), existing=self._existing)
        self.sup = Supervisor(ctx, on_start=self._on_start, on_done=self._on_done)
        self.sup.start()
        self._start_drainer()
        return True

    def shutdown(self, wait: float = 5) -> None:
        sup, self.sup = self.sup, None
        if sup is not None:
            sup.stop(wait=wait)
        if self.pool is not None:
            self.pool.close()
            self.pool = None
        if self.drainer is not None:
            self.drainer.stop()
            self.drainer = None
        if self.cache is not None:
            self.cache.close()
            self.cache = None

    def start(self, manual: bool = True) -> bool:
        """Fetch games now, and every stage after it. Returns False if setup is incomplete."""
        if mw.col is None:
            return False
        cfg = settings.load()
        if not settings.configured(cfg):
            if manual:
                from .ui import open_settings

                open_settings(first_run=not cfg.get("setup_done"))
            return False
        if self.sup is None and not self.launch():
            return False
        assert self.sup is not None
        if manual:
            self.sup.update()
        else:
            self.sup.trigger("extract")
        self._emit("start", 0, 0, "Starting…", force=True)
        return True

    def run(self, name: str) -> bool:
        """Run one stage now (and the stages after it when it has something new)."""
        if self.sup is None and not self.launch():
            return False
        assert self.sup is not None
        self.sup.trigger(name, manual=True)
        return True

    def stages(self) -> list[tuple[str, str, StageState | None]]:
        """(name, label, state) of each stage, in order; the state is None before the stages started."""
        states = self.sup.state if self.sup is not None else {}
        return [(name, LABELS[name], states.get(name)) for name in STAGES]

    def stop(self, wait: float = 0) -> None:
        """Cancel what the stages are doing now (they stay ready for their next run)."""
        sup = self.sup
        if sup is None:
            return
        self._emit("done", 0, 0, "Cancelling…", force=True)
        th = threading.Thread(target=sup.cancel_running, name="knightly-cancel", daemon=True)
        th.start()
        if wait:
            th.join(wait)

    def reschedule(self) -> None:
        """The settings changed: the stages start again with them."""
        if self.sup is not None:
            self.shutdown()
            self.launch()

    def _existing(self) -> dict[str, str] | None:
        """Stage thread: the collection's Knightly notes, read on the main thread (None: no collection)."""
        box: list = []
        done = threading.Event()

        def get() -> None:
            try:
                box.append(kc.existing_notes(mw.col) if mw.col is not None else None)
            except Exception as e:  # raised again in the stage's thread
                log.exception("could not read the collection's notes")
                box.append(e)
            finally:
                done.set()

        mw.taskman.run_on_main(get)
        while not done.wait(0.2):
            sup = self.sup
            if sup is None or sup.ctx.cancel.is_set():  # the profile is closing: the main thread may wait for us
                raise Cancelled()
        if isinstance(box[0], Exception):
            raise box[0]
        return box[0]

    def _on_start(self, name: str, tracker: Tracker) -> None:
        label = LABELS[name]
        self.tracker = tracker
        tracker.listen(
            legacy_listener(lambda stage, d, t, msg: self._emit(stage, d, t, f"{label}: {msg}" if msg else msg))
        )

    def _on_done(self, name: str, out: Outcome | None, error: str) -> None:
        label = LABELS[name]
        self.finished[name] = time.time()
        if error == "cancelled":
            self._emit("done", 0, 0, f"{label}: cancelled.", force=True)
        elif error:
            self._emit("error", 0, 0, f"{label} failed: {error}", force=True)
        elif out is not None:
            text = out.summary + (" Warnings: " + "; ".join(out.warnings) if out.warnings else "")
            self._emit("done", 1, 1, f"{label}: {text}", force=True)
            if out.notes:
                mw.taskman.run_on_main(self.drain)

    # --- the outbox → the collection
    def _start_drainer(self) -> None:
        if self.drainer is None:
            self.drainer = QTimer(mw)
            self.drainer.timeout.connect(self.drain)
            self.drainer.start(DRAIN_MS)
        self.drain()

    def drain(self) -> None:
        """Write what waits in the outbox into the collection, as one undo step (main thread)."""
        cache = self.cache
        if self._draining or cache is None or mw.col is None:
            return
        items = cache.outbox()
        if not items:
            return
        self._draining = True
        deck = settings.load().get("deck") or kc.MODEL
        new_cards = sum(d.get("new_cards", 0) for _s, kind, d in items if kind == NOTE)

        def write(col):
            kc.ensure_installed(col)
            writer = kc.RunWriter(col, deck)
            for _seq, kind, data in items:
                if kind == NOTE:
                    writer.add(NoteSpec(**data))
                elif kind == SUSPEND:
                    writer.suspend(data)
            changes = writer.finish()
            cache.outbox_done(seq for seq, _k, _d in items)
            self._wrote = writer
            return changes

        def ok(_changes) -> None:
            self._draining = False
            suspended = self._wrote.suspended if self._wrote is not None else 0
            parts = []
            if new_cards:
                parts.append(f"{new_cards} new card{'s' if new_cards != 1 else ''}")
            if suspended:
                parts.append(f"{suspended} card{'s' if suspended != 1 else ''} of out-of-date notes suspended")
            if parts:
                tooltip("Knightly Review: " + ", ".join(parts), period=5000)
                self.log("Written into the collection: " + ", ".join(parts) + ".")

        def failed(e: Exception) -> None:
            self._draining = False
            log.error("could not write the notes: %s", e)
            self._emit("error", 0, 0, f"Could not add the cards: {e}", force=True)

        CollectionOp(parent=mw, op=write).success(ok).failure(failed).run_in_background()


updater = Host()


def _on_profile_open() -> None:
    cfg = settings.load()
    try:  # card templates and styling of a new add-on version reach the collection now, not at the next update
        if mw.col is not None and mw.col.models.by_name(kc.MODEL) is not None:
            changes = kc.ensure_installed(mw.col)
            if changes:
                updater.log("Card templates: " + "; ".join(changes))
    except Exception:
        log.exception("could not set up the collection")
    if not cfg.get("setup_done"):
        from .ui import open_settings

        QTimer.singleShot(1500, lambda: open_settings(first_run=True))
    elif settings.configured(cfg):
        QTimer.singleShot(START_DELAY_MS, updater.launch)


def _on_profile_close() -> None:
    updater.shutdown(wait=5)


def setup() -> None:
    gui_hooks.profile_did_open.append(_on_profile_open)
    gui_hooks.profile_will_close.append(_on_profile_close)

"""Runs updates in the background and schedules automatic ones.

The long part (download + engine analysis) runs in a plain thread with no
collection access, so reviewing stays responsive and the job can be cancelled
when the profile closes. Notes are written afterwards on the main thread as one
undoable CollectionOp.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from typing import Callable, Optional

from aqt import gui_hooks, mw
from aqt.operations import CollectionOp
from aqt.qt import QTimer
from aqt.utils import showWarning, tooltip

from . import collection as kc
from . import settings
from .vendor.knightly.cache import SqliteCache
from .vendor.knightly.jobs import UpdateResult, run_update

Listener = Callable[[str, int, int, str], None]


class Updater:
    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.cancel = threading.Event()
        self.listeners: list[Listener] = []
        self.last: tuple[str, int, int, str] = ("idle", 0, 0, "No update has run yet.")
        # The current analysis step (positions done, total, what the step is).
        self.positions: tuple[int, int, str] = (0, 0, "")
        # Everything the run reported, for the window's log (also what happened before it was opened).
        # Entries are (sequence number, line) so a reader can pick up only the new ones.
        self.history: deque[tuple[int, str]] = deque(maxlen=1000)
        self._seq = 0
        self._logged = ""
        self._lock = threading.Lock()
        self.timer: Optional[QTimer] = None
        self._last_emit = 0.0
        self._pending = False
        # Cleared while a card is being played on with the live engine: analysis then waits.
        self.gate = threading.Event()
        self.gate.set()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

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

    def start(self, manual: bool) -> bool:
        """Start an update. Returns False if one is running or setup is incomplete."""
        if self.running or mw.col is None:
            return False
        cfg = settings.load()
        if not settings.configured(cfg):
            if manual:
                from .ui import open_settings

                open_settings(first_run=not cfg.get("setup_done"))
            return False
        try:
            changes = kc.ensure_installed(mw.col)
            existing = kc.existing_notes(mw.col)
        except Exception as e:
            if manual:
                showWarning(f"Knightly Review could not prepare the collection:\n{e}")
            return False
        core = settings.core_config(cfg)
        deck = cfg.get("deck") or kc.MODEL
        self.cancel = threading.Event()
        cancel = self.cancel
        self.positions, self._logged = (0, 0, ""), ""
        self.log(f"── {'Update' if manual else 'Automatic update'} started ──")
        self._emit("start", 0, 0, "Starting…" + (f" ({'; '.join(changes)})" if changes else ""), force=True)

        def work() -> None:
            cache = SqliteCache(settings.CACHE_PATH)
            try:
                res = run_update(core, cache, existing=existing, progress=self._emit, cancel=cancel, force=manual,
                                 gate=self.gate)
                err = None
            except Exception as e:  # reported on the main thread
                res, err = None, (e, traceback.format_exc())
            finally:
                cache.close()
            mw.taskman.run_on_main(lambda: self._finish(res, err, deck, manual))

        self.thread = threading.Thread(target=work, name="knightly-update", daemon=True)
        self.thread.start()
        return True

    def stop(self, wait: float = 0) -> None:
        self.cancel.set()
        if wait and self.thread is not None:
            self.thread.join(wait)

    def _finish(self, res: Optional[UpdateResult], err, deck: str, manual: bool) -> None:
        if err is not None:
            e, tb = err
            print(tb)
            self._emit("error", 0, 0, f"Update failed: {e}", force=True)
            if manual:
                showWarning(f"Knightly Review update failed:\n{e}")
            return
        assert res is not None
        summary = _summary(res)
        if res.cancelled:
            self._emit("done", 0, 0, "Cancelled. " + summary, force=True)
            return
        if (not res.notes and not res.order) or mw.col is None:
            self._emit("done", 1, 1, summary, force=True)
            if manual or res.warnings:
                tooltip("Knightly Review: " + summary, period=5000)
            return

        def done(_changes) -> None:
            hint = mw.col and kc.sibling_bury_hint(mw.col, deck, res.new_cards, len(res.notes))
            self._emit("done", 1, 1, summary + (" " + hint if hint else ""), force=True)
            if res.notes or manual:
                tooltip(f"Knightly Review: {res.new_cards} new card{'s' if res.new_cards != 1 else ''}", period=5000)

        CollectionOp(parent=mw, op=lambda col: kc.apply_notes(col, res.notes, deck, res.order)).success(done).failure(
            lambda e: self._emit("error", 0, 0, f"Could not add the cards: {e}", force=True)
        ).run_in_background()

    # scheduling
    def reschedule(self) -> None:
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        hours = float(settings.load().get("update_every_hours") or 0)
        if hours > 0:
            self.timer = QTimer(mw)
            self.timer.timeout.connect(lambda: self.start(manual=False))
            self.timer.start(int(hours * 3600 * 1000))


def _summary(res: UpdateResult) -> str:
    if res.skipped:
        return "No new games since the last update."
    parts = [
        f"{res.new_games} new game{'s' if res.new_games != 1 else ''}",
        f"{res.analysed_positions} position{'s' if res.analysed_positions != 1 else ''} analysed",
        f"{res.new_cards} new card{'s' if res.new_cards != 1 else ''}",
    ]
    text = ", ".join(parts) + "."
    if res.warnings:
        text += " Warnings: " + "; ".join(res.warnings)
    return text


updater = Updater()


def _on_profile_open() -> None:
    cfg = settings.load()
    updater.reschedule()
    if not cfg.get("setup_done"):
        from .ui import open_settings

        QTimer.singleShot(1500, lambda: open_settings(first_run=True))
    elif cfg.get("update_on_startup") and settings.configured(cfg):
        QTimer.singleShot(20_000, lambda: updater.start(manual=False))


def _on_profile_close() -> None:
    from .live import service

    if updater.timer is not None:
        updater.timer.stop()
    updater.gate.set()
    updater.stop(wait=5)
    service.close()


def setup() -> None:
    gui_hooks.profile_did_open.append(_on_profile_open)
    gui_hooks.profile_will_close.append(_on_profile_close)

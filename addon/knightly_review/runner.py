"""Runs updates in the background and schedules automatic ones.

The long part (download + engine analysis) runs in a plain thread with no
collection access, so reviewing stays responsive and the job can be cancelled
when the profile closes. Each note is written on the main thread as soon as its card is
built (kc.RunWriter); when the run ends, the study order is fixed and the writes become one undo step.
"""

from __future__ import annotations

import math
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
from .vendor.knightly.jobs import RemakeResult, UpdateResult, remake_notes, run_update

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
        # "Keep analysing" (settings.keep_running): after an update made its cards, more rounds follow
        # until this time, `stopped`, or nothing is left. Stopping also ends the rounds.
        self.keep_until = math.inf
        self.stopped = False
        self.keeping = False  # a keep-running session is under way (also between two rounds)

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

    def start(self, manual: bool, _round: str = "") -> bool:
        """Start an update. Returns False if one is running or setup is incomplete.
        `_round`: "", or what a keep-running session continues with: "more" (another batch of cards) or
        "precalc" (analyse and save candidates, no cards)."""
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
        if not _round and kc.remake_due(mw.col):
            # The cards were made by an older version: bring them up to date before adding new ones.
            self.log("The cards come from an older version of Knightly Review.")
            return self.remake(then_update=manual)
        core = settings.core_config(cfg)
        deck = cfg.get("deck") or kc.MODEL
        if not _round:
            self.stopped = False
            hours = core.keep_running_hours if core.keep_running in ("precalculate", "unlimited") else 0
            self.keep_until = time.monotonic() + hours * 3600 if hours > 0 else math.inf
            self.keeping = core.keep_running in ("precalculate", "unlimited")
        left_min = (self.keep_until - time.monotonic()) / 60
        if _round == "precalc":  # runs for the rest of the time, or until everything is at full depth
            core.analysis_minutes = max(0.05, left_min) if left_min != math.inf else 0
        elif _round and left_min != math.inf:
            core.analysis_minutes = min(core.analysis_minutes or left_min, max(0.05, left_min))
        self.cancel = threading.Event()
        cancel = self.cancel
        writer = kc.RunWriter(mw.col, deck)
        self.positions, self._logged = (0, 0, ""), ""
        self.log("── " + {"more": "Next batch of cards", "precalc": "Pre-calculating for later updates"}.get(
            _round, "Update" if manual else "Automatic update") + " ──")
        self._emit("start", 0, 0, "Starting…" + (f" ({'; '.join(changes)})" if changes else ""), force=True)

        def on_note(spec) -> None:
            """Analysis thread: hand the note to the main thread and wait, so writes stay in order."""
            done, failure = threading.Event(), []

            def write() -> None:
                try:
                    if mw.col is not None and not cancel.is_set():
                        writer.add(spec)
                except Exception as e:  # raised again in the analysis thread
                    failure.append(e)
                finally:
                    done.set()

            mw.taskman.run_on_main(write)
            while not done.wait(0.2):
                if cancel.is_set():  # the profile is closing and the main thread may be waiting for us
                    return
            if failure:
                raise failure[0]

        def work() -> None:
            cache = SqliteCache(settings.CACHE_PATH)
            try:
                res = run_update(core, cache, existing=existing, progress=self._emit, cancel=cancel,
                                 force=manual and not _round, gate=self.gate, precalculate=_round == "precalc",
                                 fetch=_round == "", on_note=on_note)
                err = None
            except Exception as e:  # reported on the main thread
                res, err = None, (e, traceback.format_exc())
            finally:
                cache.close()
            mw.taskman.run_on_main(lambda: self._finish(res, err, deck, manual, core.keep_running, _round, writer))

        self.thread = threading.Thread(target=work, name="knightly-update", daemon=True)
        self.thread.start()
        return True

    def remake(self, then_update: Optional[bool] = None) -> bool:
        """Rebuild the cards already in the collection with the current code (same cloze numbers, so the
        scheduling stays). A card that would no longer be made is suspended and its note tagged.
        `then_update`: None for a remake on its own; otherwise an update (manual if True) follows it.
        Returns False if an update is running or setup is incomplete."""
        if self.running or mw.col is None:
            return False
        cfg = settings.load()
        if not settings.configured(cfg):
            if then_update is None:
                from .ui import open_settings

                open_settings(first_run=not cfg.get("setup_done"))
            return False
        try:
            kc.ensure_installed(mw.col)
            existing = kc.existing_notes(mw.col)
        except Exception as e:
            showWarning(f"Knightly Review could not prepare the collection:\n{e}")
            return False
        core = settings.core_config(cfg)
        self.stopped = self.keeping = False
        self.cancel = threading.Event()
        cancel = self.cancel
        self.positions, self._logged = (0, 0, ""), ""
        self.log("── Remake the existing cards ──")
        self._emit("start", 0, 0, f"Remaking the cards of {len(existing)} notes…", force=True)

        def work() -> None:
            cache = SqliteCache(settings.CACHE_PATH)
            try:
                res = remake_notes(core, cache, existing, progress=self._emit, cancel=cancel, gate=self.gate)
                err = None
            except Exception as e:
                res, err = None, (e, traceback.format_exc())
            finally:
                cache.close()
            mw.taskman.run_on_main(lambda: self._finish_remake(res, err, then_update))

        self.thread = threading.Thread(target=work, name="knightly-remake", daemon=True)
        self.thread.start()
        return True

    def _finish_remake(self, res: Optional[RemakeResult], err, then_update: Optional[bool] = None) -> None:
        if err is not None or res is None or mw.col is None:
            e, tb = err or (RuntimeError("the collection is closed"), "")
            print(tb)
            self._emit("error", 0, 0, f"Remake failed: {e}", force=True)
            if then_update is not False:
                showWarning(f"Knightly Review remake failed:\n{e}")
            return
        summary = (f"{res.remade} of {res.cards} cards rebuilt in {len(res.pgns)} notes, "
                   f"{sum(len(v) for v in res.deprecated.values())} suspended as deprecated.")
        if res.warnings:
            summary += " Warnings: " + "; ".join(res.warnings[:5]) + (" …" if len(res.warnings) > 5 else "")
        if res.cancelled:  # nothing was written: the run is all-or-nothing, so a cancel changes nothing
            self._emit("done", 0, 0, "Cancelled, no card was changed.", force=True)
            return

        def done(_changes) -> None:
            if mw.col is not None:
                kc.mark_remade(mw.col)
            self._emit("done", 1, 1, summary, force=True)
            if then_update is None:
                tooltip("Knightly Review: " + summary, period=5000)
            else:  # the update that found the cards outdated goes on now
                QTimer.singleShot(300, lambda: self.start(manual=then_update))  # once the thread has ended

        def failed(e: Exception) -> None:
            self._emit("error", 0, 0, f"Could not write the remade cards: {e}", force=True)

        if not res.pgns and not res.deprecated:
            done(None)
            return
        CollectionOp(parent=mw, op=lambda col: kc.apply_remake(col, res.pgns, res.deprecated)).success(
            done).failure(failed).run_in_background()

    def stop(self, wait: float = 0) -> None:
        self.stopped = True
        if not self.running:  # between two rounds: nothing to cancel, just end the session
            self._end_keeping()
        self.cancel.set()
        if wait and self.thread is not None:
            self.thread.join(wait)

    def _next_round(self, res: UpdateResult, mode: str, round_: str) -> None:
        """After a round's cards are in the collection: carry on, or end the keep-running session."""
        again = ""
        if mode in ("precalculate", "unlimited") and self.keeping and not self.stopped and not res.cancelled \
                and time.monotonic() < self.keep_until:
            if round_ == "precalc":
                again = ""  # one pass up to the time limit or full depth
            elif mode == "precalculate":
                again = "precalc"
            elif not res.skipped and (res.new_cards or not res.finished):
                again = "more"
        if again and mw.col is not None:
            QTimer.singleShot(500, lambda: self._start_round(again))
        else:
            self._end_keeping(res)

    def _start_round(self, round_: str) -> None:
        """The timer's side of `_next_round`: stop, closing the profile or another update may have happened since."""
        if self.stopped or not self.keeping or mw.col is None:
            self._end_keeping()
        elif self.running:  # an update started by hand or the schedule took over; it resets the session
            self.keeping = False
        elif not self.start(manual=False, _round=round_):
            self._end_keeping()

    def _end_keeping(self, res: Optional[UpdateResult] = None) -> None:
        if self.keeping:
            self.keeping = False
            self._emit("done", 1, 1, "Finished analysing in the background." if res is not None and not res.cancelled
                       else "Stopped analysing in the background.", force=True)

    def _finish(self, res: Optional[UpdateResult], err, deck: str, manual: bool, mode: str = "off",
                round_: str = "", writer: Optional[kc.RunWriter] = None) -> None:
        """The run is over: fix the study order, merge what it wrote into one undo step, then report."""
        order = res.order if res is not None and not res.cancelled else []

        def failed(e: Exception) -> None:
            self.keeping = False
            self._emit("error", 0, 0, f"Could not add the cards: {e}", force=True)

        if writer is None or (not writer.written and not order) or mw.col is None:
            self._report(res, err, deck, manual, mode, round_)
            return
        CollectionOp(parent=mw, op=lambda col: writer.finish(order)).success(
            lambda _changes: self._report(res, err, deck, manual, mode, round_)).failure(failed).run_in_background()

    def _report(self, res: Optional[UpdateResult], err, deck: str, manual: bool, mode: str, round_: str) -> None:
        if err is not None:
            self.keeping = False
            e, tb = err
            print(tb)
            self._emit("error", 0, 0, f"Update failed: {e}", force=True)
            if manual:
                showWarning(f"Knightly Review update failed:\n{e}")
            return
        assert res is not None
        summary = _summary(res)
        if res.cancelled:
            self.keeping = False
            self._emit("done", 0, 0, "Cancelled. " + summary, force=True)
            return
        hint = mw.col and kc.sibling_bury_hint(mw.col, deck, res.new_cards, len(res.notes))
        self._emit("done", 1, 1, summary + (" " + hint if hint else ""), force=True)
        if not round_:
            if res.notes or (manual and res.order):
                tooltip(f"Knightly Review: {res.new_cards} new card{'s' if res.new_cards != 1 else ''}", period=5000)
            elif manual or res.warnings:
                tooltip("Knightly Review: " + summary, period=5000)
        self._next_round(res, mode, round_)

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
        text = "Nothing changed since the last update."
        return text + (" Warnings: " + "; ".join(res.warnings) if res.warnings else "")
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
    try:  # card templates and styling of a new add-on version reach the collection now, not at the next update
        if mw.col is not None and kc.MODEL in mw.col.models.all_names():
            changes = kc.ensure_installed(mw.col)
            if changes:
                updater.log("Card templates: " + "; ".join(changes))
    except Exception:
        traceback.print_exc()
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

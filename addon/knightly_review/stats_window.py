"""Tools → Knightly Review → Statistics: the dashboard page (core/knightly/dashboard.py) in a window, and saved as
an HTML file. The numbers are read from the cache on a thread of their own; Refresh reads them again."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from aqt import mw
from aqt.qt import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWebEngineView
from aqt.theme import theme_manager
from aqt.utils import showWarning, tooltip

from . import settings
from .vendor.knightly import dashboard
from .vendor.knightly.cache import SqliteCache

log = logging.getLogger(__name__)


def _collect() -> dashboard.Stats:
    cache = SqliteCache(settings.CACHE_PATH)
    try:
        return dashboard.collect(settings.core_config(), cache)
    finally:
        cache.close()


class StatsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Knightly Review statistics")
        self.stats: dashboard.Stats | None = None
        self.view = QWebEngineView(self)
        self.status = QLabel("Reading your games…")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        self.save_btn = QPushButton("Save as HTML…")
        self.save_btn.clicked.connect(self.save)
        self.save_btn.setEnabled(False)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        for b in (refresh, self.save_btn, close):
            buttons.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addLayout(buttons)
        self.resize(860, 760)
        self.refresh()

    def _theme(self) -> str:
        return "dark" if theme_manager.night_mode else "light"

    def refresh(self) -> None:
        self.status.setText("Reading your games…")

        def run() -> None:
            try:
                st = _collect()
            except Exception as e:  # shown in the window
                log.exception("could not read the statistics")
                err = e
                mw.taskman.run_on_main(lambda: self._failed(err))
                return
            mw.taskman.run_on_main(lambda: self._show(st))

        threading.Thread(target=run, name="knightly-stats", daemon=True).start()

    def _show(self, st: dashboard.Stats) -> None:
        self.stats = st
        self.view.setHtml(dashboard.render(st, self._theme()))
        self.status.setText("")
        self.save_btn.setEnabled(True)

    def _failed(self, e: Exception) -> None:
        self.status.setText(f"Could not read the statistics: {e}")

    def save(self) -> None:
        if self.stats is None:
            return
        cfg = settings.load()
        start = str(Path(cfg.get("export_dir") or Path.home()).expanduser() / "knightly-statistics.html")
        path, _ = QFileDialog.getSaveFileName(self, "Save the statistics", start, "Web page (*.html)")
        if not path:
            return
        try:
            Path(path).write_text(dashboard.render(self.stats), encoding="utf-8")  # follows the reader's theme
        except OSError as e:
            showWarning(f"Could not save the statistics: {e}", parent=self)
            return
        tooltip(f"Saved {Path(path).name}.", period=3000)


_window: StatsWindow | None = None


def show_stats() -> None:
    global _window
    if _window is None:
        _window = StatsWindow(mw)
    else:
        _window.refresh()
    _window.show()
    _window.raise_()
    _window.activateWindow()

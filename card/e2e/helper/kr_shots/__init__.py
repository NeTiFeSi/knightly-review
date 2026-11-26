"""Test-only add-on for the README screenshots (card/e2e/screenshots.mjs). NOT part of Knightly Review.

Install it in the isolated test profile only (symlink into its addons21/). It listens on
127.0.0.1:8767 and, on `GET /shot?what=settings|engine|progress|live[&wait=ms]&file=/abs/path.png`, opens that
Knightly Review window with sample data and saves a picture of it (Qt's own widget.grab(), so no
screen capture and no window decorations).
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from aqt import gui_hooks, mw

PORT = 8767

SAMPLE_LOG = [
    "Fetching games from Lichess…",
    "12 new games from Lichess, 3 from Chess.com",
    "Analysing 12 games (depth 13)…",
    "Game 4 of 12: 2 mistakes found",
    "Game 9 of 12: 1 mistake found",
]


def _make(what: str):
    from knightly_review import ui
    from knightly_review.runner import updater

    if what == "settings":
        return ui.SettingsDialog(mw)
    if what == "engine":
        return ui.EngineDialog(mw)
    if what == "live":  # a real update on the test profile's settings, grabbed while it runs
        ui.start_update()
        return ui._progress
    if what == "progress":
        updater.last = ("analyse", 7, 20, "Looking for puzzles in game 9 of 12…")
        updater.positions = (31, 64, "Checking the best alternatives (deep analysis)")
        for line in SAMPLE_LOG:
            updater.log(line)
        return ui.ProgressWindow(mw)
    raise ValueError(what)


def _tidy(w) -> None:
    """English button labels whatever the system language, and no home folder in the picture."""
    import os

    from aqt.qt import QDialogButtonBox, QLabel

    home = os.path.expanduser("~")
    for bb in w.findChildren(QDialogButtonBox):
        for b, text in ((QDialogButtonBox.StandardButton.Save, "Save"), (QDialogButtonBox.StandardButton.Ok, "OK"),
                        (QDialogButtonBox.StandardButton.Cancel, "Cancel")):
            btn = bb.button(b)
            if btn:
                btn.setText(text)
    for lab in w.findChildren(QLabel):
        if home in lab.text():
            lab.setText(lab.text().replace(home, "~").replace("Anki2-knightly-test", "Anki2"))
    w.resize(max(w.width(), 560), 10)
    w.adjustSize()
    w.resize(max(w.width(), 560), w.height())


def _grab(what: str, file: str, done: dict, wait: int = 1500) -> None:
    try:
        w = _make(what)
        w.show()
        w.raise_()
        _tidy(w)

        def snap() -> None:
            try:
                done["ok"] = w.grab().save(file)
            finally:
                w.close()
                done["event"].set()

        # Give it a moment to lay out and for background scans (the engine search) to finish.
        mw.progress.single_shot(wait, snap)
    except Exception as e:  # noqa: BLE001
        done["error"] = repr(e)
        done["event"].set()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path != "/shot" or "what" not in q or "file" not in q:
            self.send_response(404)
            self.end_headers()
            return
        done = {"event": threading.Event()}
        wait = int(q.get("wait", 1500))
        mw.taskman.run_on_main(lambda: _grab(q["what"], q["file"], done, wait))
        done["event"].wait(30 + wait / 1000)
        body = ("ok" if done.get("ok") else done.get("error", "failed")).encode()
        self.send_response(200 if done.get("ok") else 500)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:
        pass


def _start() -> None:
    try:
        srv = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()


gui_hooks.profile_did_open.append(_start)

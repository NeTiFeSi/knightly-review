"""Test-only add-on for the README screenshots (card/e2e/screenshots.mjs). NOT part of Knightly Review.

Install it in the isolated test profile only (symlink into its addons21/). It listens on
127.0.0.1:8767 and, on `GET /shot?what=settings|engine|progress|live|stats[&wait=ms]&file=/abs/path.png`, opens that
Knightly Review window with sample data and saves a picture of it (Qt's own widget.grab(), so no
screen capture and no window decorations).
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from aqt import gui_hooks, mw

PORT = 8767
log = logging.getLogger(__name__)

SAMPLE_LOG = [
    "Fetching games from Lichess…",
    "12 new games from Lichess, 3 from Chess.com",
    "Collecting the positions of 15 games…",
    "Ply 3, your moves: analysing 15 positions at depth 13",
    "Ply 4, your moves: analysing 14 positions at depth 13",
    "Ply 5, your moves: analysing 12 positions at depth 13",
]

SAMPLE_CONFIG = {  # what the settings window shows in the README: the defaults, with made-up names
    "lichess_user": "your-lichess-name",
    "chesscom_user": "your-chesscom-name",
    "deck": "Knightly Review",
    "stockfish_path": "/usr/bin/stockfish",
    "sf_depth": 13,
    "threads": 0,
    "hash_mb": 64,
}


def _make(what: str):
    from knightly_review import ui
    from knightly_review.runner import updater

    if what == "stats":  # the real statistics of the test profile's games
        from knightly_review.stats_window import StatsWindow

        return StatsWindow(mw)
    if what == "settings":
        from knightly_review import settings

        real = settings.load
        settings.load = lambda: {**real(), **SAMPLE_CONFIG, "analysis_minutes": 10, "max_new_cards": 10}
        try:
            return ui.SettingsDialog(mw)
        finally:
            settings.load = real
    if what == "engine":
        return ui.EngineDialog(mw)
    if what == "live":  # a real update on the test profile's settings, grabbed while it runs
        ui.start_update()
        return ui._progress
    if what == "progress":
        # A run in the middle of ply 5, as the real tracker reports it: about 3.7 s per position at depth 13.
        from knightly_review.vendor.knightly.progress import Tracker

        now = [0.0]
        t = updater.tracker = Tracker(clock=lambda: now[0])
        fen = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 5 4"
        step = "Ply 5, your moves: analysing 12 positions at depth 13"
        message = f"{step} (7 of 20 cards made · 6 min left)"
        updater.last = ("analyse", 7, 20, message)
        t.engine("stockfish", "Stockfish 19", True)
        t.plies(5, 38)
        t.found(7, 20)
        t.budget(6.0)
        t.stage("analyse", message, total=12, unit="positions", step="depth 13")
        for _ in range(7):
            t.search_start("stockfish", fen, 5, 28, 13)
            now[0] += 3.7
            t.search_end("stockfish", ms=3700)
            t.advance()
        t.position("ply 5: position 8 of 12")
        t.search_start("stockfish", fen, 5, 28, 13)
        t.search_progress("stockfish", depth=10, nodes=3_000_000, nps=1_400_000, ms=2100)
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
        for b, text in (
            (QDialogButtonBox.StandardButton.Save, "Save"),
            (QDialogButtonBox.StandardButton.Ok, "OK"),
            (QDialogButtonBox.StandardButton.Cancel, "Cancel"),
        ):
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
        if what != "stats":  # its size is its own: the page scrolls
            _tidy(w)

        def snap() -> None:
            try:
                done["ok"] = w.grab().save(file)
            finally:
                w.close()
                done["event"].set()

        # Give it a moment to lay out and for background scans (the engine search) to finish.
        mw.progress.single_shot(wait, snap)
    except Exception as e:
        log.exception("could not take the picture of %s", what)
        done["error"] = repr(e)
        done["event"].set()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
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

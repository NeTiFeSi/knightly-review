"""Live engine for cards on desktop: answers pycmd('kr:engine:{…}') from the reviewer.

Requests run one at a time in a worker thread with their own engine processes
(the same Stockfish/Lc0 settings as the analysis). Each request pauses the
background update for a while, so reviewing gets the CPU. The answer goes back
to the card with web.eval(window.__krEngine.resolve(id, …)).
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from typing import Any, Optional

from aqt import mw
from aqt.qt import QTimer

from . import settings
from .vendor.knightly.cache import SqliteCache
from .vendor.knightly.config import Config
from .vendor.knightly.engine.analyse import Analyser
from .vendor.knightly.live import query

PAUSE_MS = 30_000  # background analysis waits this long after the last live request


def engine_label(cfg: dict[str, Any]) -> Optional[str]:
    """What the card shows as the live engine, or None when none is set up."""
    if not cfg.get("stockfish_path"):
        return None
    return "Stockfish + Lc0" if cfg.get("use_lc0") and cfg.get("lc0_path") else "Stockfish"


class LiveService:
    def __init__(self) -> None:
        self.jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self._analyser: Optional[Analyser] = None
        self._cfg_key: Optional[tuple] = None
        self._resume: Optional[QTimer] = None

    # main thread -------------------------------------------------------------
    def submit(self, req: dict[str, Any], web) -> None:
        cfg = settings.core_config()
        self._pause_background()
        self.jobs.put((req, web, cfg))
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._work, name="knightly-live", daemon=True)
            self.thread.start()

    def warm(self) -> None:
        """Start the engines early (Lc0 takes a few seconds), when a card first asks for one."""
        if self.thread is None or not self.thread.is_alive():
            self.jobs.put(({"id": 0, "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"}, None,
                           settings.core_config()))
            self.thread = threading.Thread(target=self._work, name="knightly-live", daemon=True)
            self.thread.start()

    def _pause_background(self) -> None:
        from .runner import updater

        updater.gate.clear()
        if self._resume is None:
            self._resume = QTimer(mw)
            self._resume.setSingleShot(True)
            self._resume.timeout.connect(updater.gate.set)
        self._resume.start(PAUSE_MS)

    def close(self) -> None:
        self.jobs.put(None)
        if self.thread is not None:
            self.thread.join(5)
        self.thread = None

    # worker thread -----------------------------------------------------------
    def _analyser_for(self, cfg: Config) -> Analyser:
        key = (cfg.stockfish_path, cfg.sf_depth, cfg.threads, cfg.hash_mb, cfg.use_lc0, cfg.lc0_path, cfg.lc0_time)
        if self._analyser is None or key != self._cfg_key:
            if self._analyser is not None:
                self._analyser.close()
            self._analyser = Analyser(cfg, SqliteCache(settings.CACHE_PATH))
            self._cfg_key = key
        return self._analyser

    def _work(self) -> None:
        try:
            while True:
                item = self.jobs.get()
                if item is None:
                    break
                req, web, cfg = item
                try:
                    lines = query(self._analyser_for(cfg), req["fen"], int(req.get("multipv", 1)), req.get("moves") or None)
                    result: dict[str, Any] = {"lines": lines}
                except Exception as e:  # reported to the card, which then stops playing on
                    traceback.print_exc()
                    result = {"error": str(e)}
                js = f"window.__krEngine && window.__krEngine.resolve({int(req['id'])}, {json.dumps(result)});"
                mw.taskman.run_on_main(lambda js=js, web=web: _eval(web, js))
        finally:
            if self._analyser is not None:
                self._analyser.close()
                self._analyser = None


def _eval(web, js: str) -> None:
    if web is None:  # a warm-up request
        return
    try:
        web.eval(js)
    except Exception:
        pass  # the reviewer moved on


service = LiveService()


def handle(message: str, context: Any) -> dict[str, Any]:
    """pycmd('kr:engine:{json}') → {pending: true}, with the answer delivered later."""
    web = getattr(context, "web", None) or getattr(context, "_web", None)
    if web is None:
        return {"error": "no card view"}
    if not engine_label(settings.load()):
        return {"error": "no engine configured in Knightly Review's settings"}
    try:
        req = json.loads(message[len("kr:engine:"):])
        req["id"] = int(req["id"])
        if not isinstance(req.get("fen"), str):
            raise ValueError("fen missing")
    except (ValueError, KeyError, TypeError) as e:
        return {"error": f"bad request: {e}"}
    service.submit(req, web)
    return {"pending": True}

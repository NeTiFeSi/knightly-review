"""`knightly daemon`: the stages in a process of their own, next to Anki.

It runs extract, annotate, select and build on the add-on's cache, with the add-on's settings (read again when
they change), so puzzles are ready before Anki opens. It has no collection: it knows the notes on cards from
what the add-on publishes into the cache (`publish_notes`, and at every outbox drain), plus the outbox. Manage,
which rewrites and suspends notes, runs only in the add-on. Notes go to the outbox, which the add-on writes into
the collection when it runs.

The two take turns (supervisor.Turn): while Anki runs the add-on's stages, the daemon waits; when Anki opens,
the daemon hands over after its current search.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .cache import SqliteCache
from .config import Config, upgrade
from .engine.broker import Lease, Pool
from .progress import Tracker
from .supervisor import Context, Outcome, Supervisor, Turn

STAGES = ("extract", "annotate", "select", "build")
LABELS = {"extract": "Games", "annotate": "Annotating", "select": "Candidates", "build": "Puzzles"}
RELOAD_SECONDS = 10


class AddonFolder:
    """The add-on's folder in Anki's addons21: its settings (meta.json over config.json), secrets and cache."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder).expanduser()
        if not (self.folder / "config.json").is_file():
            raise ValueError(f"{self.folder} is not the Knightly Review add-on's folder (no config.json)")

    @property
    def cache_path(self) -> Path:
        return self.folder / "user_files" / "cache.sqlite"

    def stamp(self) -> tuple:
        """Changes when the settings or secrets do."""
        out = []
        for f in (self.folder / "meta.json", self.folder / "user_files" / "secrets.json"):
            try:
                st = f.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def config(self) -> Config:
        defaults = json.loads((self.folder / "config.json").read_text())
        try:
            stored = json.loads((self.folder / "meta.json").read_text()).get("config") or {}
        except (OSError, ValueError):
            stored = {}
        cfg = Config.from_dict(upgrade(stored, defaults))
        try:
            secrets = json.loads((self.folder / "user_files" / "secrets.json").read_text())
        except (OSError, ValueError):
            secrets = {}
        cfg.extract.lichess_token = secrets.get("lichess_token", "")
        return cfg


def daemon_config(cfg: Config) -> Config:
    """What the daemon runs with: the settings, annotating on until everything is done (that's its job)."""
    cfg.annotate.keep_annotating = True
    cfg.annotate.keep_annotating_hours = 0
    return cfg


class Daemon:
    def __init__(
        self,
        load: Callable[[], Config],
        cache: SqliteCache,
        *,
        log: Callable[[str], None] = print,
        stamp: Callable[[], object] = lambda: None,
        pool: Pool | None = None,
        workers: dict | None = None,
        analyser_factory=None,
    ):
        self.load, self.cache, self.log, self.stamp = load, cache, log, stamp
        cfg = daemon_config(load())
        self._stamp = stamp()
        self.pool = pool or Pool(cfg, lease=Lease(cache))
        ctx = Context(
            cfg, cache, self.pool, threading.Event(), existing=cache.published_notes, analyser_factory=analyser_factory
        )
        self.turn = Turn(cache, first=False)
        self.sup = Supervisor(
            ctx, on_start=self._on_start, on_done=self._on_done, workers=workers, stages=STAGES, turn=self.turn
        )
        self._waiting: bool | None = None

    def _on_start(self, name: str, tracker: Tracker) -> None:
        self.log(f"{LABELS[name]}: started")

    def _on_done(self, name: str, out: Outcome | None, error: str) -> None:
        label = LABELS[name]
        if error:
            self.log(f"{label}: {'cancelled' if error == 'cancelled' else 'failed: ' + error}")
        elif out is not None:
            self.log(f"{label}: {out.summary}" + (" Warnings: " + "; ".join(out.warnings) if out.warnings else ""))

    def start(self) -> None:
        if self.cache.published_notes() is None:
            self.log("No notes published by the add-on yet: puzzles are made after Anki has run it once.")
        self.sup.start()
        self.sup.trigger("annotate")  # the work left from before (then select and build, when it annotates)
        self.sup.trigger("select")

    def tick(self) -> None:
        """Every few seconds: new settings, and whether Anki has the turn."""
        st = self.stamp()
        if st != self._stamp:
            try:
                cfg = daemon_config(self.load())
            except (OSError, ValueError, TypeError) as e:  # a half-written file: read it again next time
                self.log(f"Could not read the settings: {e}")
            else:
                self._stamp = st
                self.sup.reconfigure(cfg)
                self.log("Settings changed: using the new ones.")
        waiting = self.turn.wanted()
        if waiting != self._waiting:
            if waiting:
                self.log("Anki is running the stages: waiting until it closes.")
            elif self._waiting:
                self.log("Anki closed: going on.")
                self.sup.trigger("annotate")  # the notes it published may open new candidates
                self.sup.trigger("select")
            self._waiting = waiting

    def stop(self, wait: float = 10) -> None:
        self.sup.stop(wait=wait)
        self.pool.close()


def run(folder: str | Path, stop: threading.Event | None = None, log: Callable[[str], None] | None = None) -> int:
    """The daemon on the add-on in `folder`, until `stop` is set (or Ctrl+C)."""
    addon = AddonFolder(folder)
    stop = stop or threading.Event()
    say = log or (lambda line: print(time.strftime("%H:%M:%S  ") + line, flush=True))
    cache = SqliteCache(addon.cache_path)
    d = Daemon(addon.config, cache, log=say, stamp=addon.stamp)
    if not d.sup.ctx.cfg.extract.has_games() or not d.sup.ctx.cfg.engine.stockfish_path:
        say("Set up Knightly Review in Anki first (an account or PGN file, and Stockfish).")
        cache.close()
        return 2
    say(f"Knightly Review daemon on {addon.folder} (Ctrl+C to stop)")
    d.start()
    try:
        while not stop.wait(RELOAD_SECONDS):
            d.tick()
    except KeyboardInterrupt:
        pass
    say("Stopping…")
    d.stop()
    cache.close()
    return 0

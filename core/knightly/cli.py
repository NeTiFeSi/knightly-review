"""Command line: `python -m knightly …`. Handy for power users, debugging and tests."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

from .cache import SqliteCache
from .config import Account, Config
from .progress import Tracker, describe


def default_cache() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "knightly")
        if sys.platform == "win32"
        else os.path.expanduser("~/.cache")
    )
    return os.path.join(base, "knightly", "cache.sqlite")


class _Bars:
    """tqdm bars on a terminal (stage, and one per engine in use), refreshed from the tracker's snapshots a few
    times a second. Needs `tqdm`; without it (or off a terminal) the plain message lines are printed."""

    def __init__(self, tracker: Tracker):
        self.tracker, self.stop_ev = tracker, threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.bars: dict = {}
        try:
            from tqdm import tqdm

            self.tqdm = tqdm
        except ImportError:
            self.tqdm = None

    def start(self) -> None:
        if self.tqdm:
            self.thread.start()

    def stop(self) -> None:
        self.stop_ev.set()
        if self.tqdm:
            self.thread.join(1)
            for b in self.bars.values():
                b.close()

    def _bar(self, key: str, position: int):
        if key not in self.bars:
            self.bars[key] = self.tqdm(total=1, position=position, leave=False, bar_format="{desc} {bar} {postfix}")
        return self.bars[key]

    def _loop(self) -> None:
        while not self.stop_ev.wait(0.2):
            d = describe(self.tracker.snapshot())
            top = self._bar("stage", 0)
            top.set_description_str((d["message"] or "…")[:90])
            top.total, top.n = 1000, int(1000 * (d["stage_ratio"] or 0))
            top.set_postfix_str(f"{d['stage']} · {d['found']} · {d['elapsed']}")
            top.refresh()
            for i, (kind, e) in enumerate(d["engines"].items(), start=1):
                b = self._bar(kind, i)
                b.set_description_str(f"{e['label']:<14}")
                b.total, b.n = 1000, int(1000 * (e["ratio"] or 0))
                b.set_postfix_str(f"{e['detail']} · {e['counts']}")
                b.refresh()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="knightly")
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="fetch, analyse and write note specs as JSON")
    up.add_argument("--lichess", action="append", default=[], help="your Lichess username (repeatable)")
    up.add_argument("--chesscom", action="append", default=[], help="your Chess.com username (repeatable)")
    up.add_argument("--pgn", action="append", default=[], help="a PGN file or folder to read games from (repeatable)")
    up.add_argument("--export", default="", help="folder to write every account's games to as PGN")
    up.add_argument("--stockfish", default="", help="Stockfish path (default: the first one found)")
    up.add_argument("--depth", type=int, help="Stockfish depth, every move searched (default 13)")
    up.add_argument("--cache", default=default_cache())
    up.add_argument("--config", help="JSON file with Config fields")
    up.add_argument("--minutes", type=float, help="engine time for this run; 0 = until enough cards are found")
    up.add_argument("--max-cards", type=int)
    up.add_argument("--days", type=int, help="look back this many days; 0 = all time")
    up.add_argument("--out", default="-", help="output JSON file ('-' = stdout)")

    dm = sub.add_parser("daemon", help="fetch, annotate and make puzzles in the background, for the Anki add-on to add")
    dm.add_argument("--addon", required=True, help="the Knightly Review add-on's folder in Anki's addons21")

    eng = sub.add_parser("engines", help="list the Stockfish builds found on this computer")
    eng.add_argument("--dir", help="extra folder with downloaded engines")

    inst = sub.add_parser("install-options", help="how to install Stockfish on this OS")
    dl = sub.add_parser("download-stockfish", help="download the official Stockfish build")
    dl.add_argument("dir")
    del inst

    args = ap.parse_args(argv)

    if args.cmd == "daemon":
        import signal

        from . import daemon

        stop = threading.Event()
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            return daemon.run(args.addon, stop)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    if args.cmd == "engines":
        from .engine.discover import discover

        for e in discover(args.dir):
            print(f"{e.name}\t{e.found_in}\t{e.path}")
        return 0
    if args.cmd == "install-options":
        from .engine.install import package_options

        for o in package_options():
            print(f"{o.manager}\t{o.command}\t{'can run for you' if o.argv else 'needs admin'}")
        return 0
    if args.cmd == "download-stockfish":
        from .engine.install import download_stockfish

        print(download_stockfish(args.dir, progress=lambda d, t: print(f"\r{d}/{t} bytes", end="", file=sys.stderr)))
        return 0

    from .engine.discover import discover
    from .jobs import run_update

    cfg = Config.from_dict(json.loads(Path(args.config).read_text()) if args.config else {})
    cfg.extract.accounts += [Account("lichess", n) for n in args.lichess] + [
        Account("chesscom", n) for n in args.chesscom
    ]
    cfg.extract.__post_init__()  # drop duplicates
    cfg.extract.pgn_paths += args.pgn
    cfg.extract.export_dir = args.export or cfg.extract.export_dir
    if args.minutes is not None:
        cfg.build.analysis_minutes = args.minutes
    if args.max_cards is not None:
        cfg.build.max_new_cards = args.max_cards
    if args.days is not None:
        cfg.extract.history_days = args.days
    if args.depth is not None:
        cfg.annotate.sf_depth = args.depth
    cfg.engine.stockfish_path = args.stockfish or cfg.engine.stockfish_path
    if not cfg.engine.stockfish_path:
        found = discover()
        if not found:
            print("Stockfish not found; pass --stockfish", file=sys.stderr)
            return 2
        cfg.engine.stockfish_path = found[0].path
    if not cfg.extract.has_games():
        print("pass --lichess, --chesscom or --pgn", file=sys.stderr)
        return 2

    def progress(stage: str, done: int, total: int, msg: str) -> None:
        if stage != "positions":  # one per position: too chatty for a terminal
            print(f"[{stage}] {msg}", file=sys.stderr)

    tracker = Tracker()
    view = _Bars(tracker) if sys.stderr.isatty() else None
    if view:
        progress = lambda *a: None
    if view:
        view.start()
    try:
        res = run_update(cfg, SqliteCache(args.cache), progress=progress, force=True, tracker=tracker)
    finally:
        if view:
            view.stop()
    out = {
        "new_games": res.new_games,
        "analysed_positions": res.analysed_positions,
        "new_cards": res.new_cards,
        "cancelled": res.cancelled,
        "warnings": res.warnings,
        "stats": res.stats,
        "notes": [n.to_json() for n in res.notes],
    }
    text = json.dumps(out, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

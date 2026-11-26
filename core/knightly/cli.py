"""Command line: `python -m knightly …`. Handy for power users, debugging and tests."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import threading

from .cache import SqliteCache
from .config import Config
from .progress import Tracker, describe


def default_cache() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "knightly") if sys.platform == "win32" else os.path.expanduser("~/.cache")
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
            top.set_postfix_str(f'{d["stage"]} · {d["found"]} · {d["elapsed"]}')
            top.refresh()
            for i, (kind, e) in enumerate(d["engines"].items(), start=1):
                b = self._bar(kind, i)
                b.set_description_str(f'{e["label"]:<14}')
                b.total, b.n = 1000, int(1000 * (e["ratio"] or 0))
                b.set_postfix_str(f'{e["detail"]} · {e["counts"]}')
                b.refresh()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="knightly")
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="fetch, analyse and write note specs as JSON")
    up.add_argument("--lichess", default="")
    up.add_argument("--chesscom", default="")
    up.add_argument("--stockfish", default="", help="Stockfish path (default: the first one found)")
    up.add_argument("--lc0", default="", help="also analyse with this Lc0, after Stockfish, for as long as it took")
    up.add_argument("--depth", type=int, help="Stockfish depth (default 13)")
    up.add_argument("--cache", default=default_cache())
    up.add_argument("--config", help="JSON file with Config fields")
    up.add_argument("--minutes", type=float, help="engine time for this run; 0 = until enough cards are found")
    up.add_argument("--max-cards", type=int)
    up.add_argument("--days", type=int, help="look back this many days; 0 = all time")
    up.add_argument("--out", default="-", help="output JSON file ('-' = stdout)")

    eng = sub.add_parser("engines", help="list the supported engines (Stockfish, Lc0) found on this computer")
    eng.add_argument("--dir", help="extra folder with downloaded engines")

    inst = sub.add_parser("install-options", help="how to install Stockfish on this OS")
    dl = sub.add_parser("download-stockfish", help="download the official Stockfish build")
    dl.add_argument("dir")
    del inst

    args = ap.parse_args(argv)

    if args.cmd == "engines":
        from .engine.discover import discover

        for e in discover(args.dir):
            print(f"{e.kind}\t{e.name}\t{e.found_in}\t{e.path}")
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
    cfg.lichess_user = args.lichess or cfg.lichess_user
    cfg.chesscom_user = args.chesscom or cfg.chesscom_user
    if args.minutes is not None:
        cfg.analysis_minutes = args.minutes
    if args.max_cards is not None:
        cfg.max_new_cards = args.max_cards
    if args.days is not None:
        cfg.history_days = args.days
    if args.depth is not None:
        cfg.sf_depth = args.depth
    cfg.stockfish_path = args.stockfish or cfg.stockfish_path
    if not cfg.stockfish_path:
        found = discover(kind="stockfish")
        if not found:
            print("Stockfish not found; pass --stockfish", file=sys.stderr)
            return 2
        cfg.stockfish_path = found[0].path
    if args.lc0:
        cfg.use_lc0, cfg.lc0_path = True, args.lc0
    if not (cfg.lichess_user or cfg.chesscom_user):
        print("pass --lichess and/or --chesscom", file=sys.stderr)
        return 2

    def progress(stage: str, done: int, total: int, msg: str) -> None:
        if stage != "positions":  # one per position: too chatty for a terminal
            print(f"[{stage}] {msg}", file=sys.stderr)

    tracker = Tracker()
    view = _Bars(tracker) if sys.stderr.isatty() else None
    if view:
        progress = lambda *a: None  # noqa: E731  (the bars show everything; the messages go above them)
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

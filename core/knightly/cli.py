"""Command line: `python -m knightly …`. Handy for power users, debugging and tests."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .cache import SqliteCache
from .config import Config


def default_cache() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "knightly") if sys.platform == "win32" else os.path.expanduser("~/.cache")
    )
    return os.path.join(base, "knightly", "cache.sqlite")


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
    up.add_argument("--by-motif", action="store_true", help="one note per tactical motif (note_grouping=motif)")
    up.add_argument("--by-opening", action="store_true", help="one note per opening and ply (note_grouping=opening)")
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
    if args.by_motif:
        cfg.note_grouping = "motif"
    if args.by_opening:
        cfg.note_grouping = "opening"
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

    res = run_update(cfg, SqliteCache(args.cache), progress=progress, force=True)
    out = {
        "new_games": res.new_games,
        "analysed_positions": res.analysed_positions,
        "new_cards": res.new_cards,
        "cancelled": res.cancelled,
        "warnings": res.warnings,
        "stats": res.stats,
        "notes": [n.to_json() for n in res.notes],
        "order": res.order,
    }
    text = json.dumps(out, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

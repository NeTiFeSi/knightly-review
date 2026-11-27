"""Games from PGN files on disk: single files, or folders searched (with their subfolders) for *.pgn.

A file is read again only when its size or modification time changed. Each game's id is a hash of its
headers and moves, so the same game in two files, or a file read twice, is stored once. Games that don't start
from the standard position, variants and games with illegal moves are skipped.
"""

from __future__ import annotations

import calendar
import hashlib
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

import chess
import chess.pgn

from .. import http
from ..model import Game

SOURCE = "pgn"


def files(paths: Iterable[str], skip: str = "") -> list[Path]:
    """The PGN files the paths name: files as they are, folders searched for *.pgn (any case). Files inside
    `skip` (the export folder, whose files hold copies of games stored already) are left out."""
    out: list[Path] = []
    for p in paths:
        path = Path(p).expanduser()
        if path.is_dir():
            out += sorted(f for f in path.rglob("*") if f.is_file() and f.suffix.lower() == ".pgn")
        elif path.is_file():
            out.append(path)
    if skip:
        root = Path(skip).expanduser().resolve()
        out = [f for f in out if not f.resolve().is_relative_to(root)]
    return list(dict.fromkeys(out))


def _header(h: chess.pgn.Headers, name: str) -> str:
    v = h.get(name, "").strip()
    return "" if v in ("?", "-", "????.??.??") else v


def _end_ts(h: chess.pgn.Headers, fallback: int) -> int:
    """When the game was played, from UTCDate/UTCTime or Date (unknown month or day count as the first);
    `fallback` when the headers don't say."""
    date = _header(h, "UTCDate") or _header(h, "Date")
    parts = date.split(".")
    try:
        year = int(parts[0])
    except (ValueError, IndexError):
        return fallback
    month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    hh = mm = ss = 0
    time_ = _header(h, "UTCTime").split(":")
    if len(time_) == 3 and all(x.isdigit() for x in time_):
        hh, mm, ss = map(int, time_)
    try:
        return calendar.timegm((year, month, day, hh, mm, ss, 0, 0, 0))
    except (ValueError, OverflowError):
        return fallback


def time_class(tc: str) -> str:
    """Lichess's names for a PGN TimeControl ("600+5"): by the estimated duration, base + 40 × increment."""
    tc = tc.strip()
    if not tc or tc == "?":
        return ""
    if tc == "-" or "/" in tc:
        return "correspondence"
    base, _, inc = tc.partition("+")
    try:
        secs = int(base) + 40 * int(inc or 0)
    except ValueError:
        return ""
    if secs < 30:
        return "ultraBullet"
    if secs < 180:
        return "bullet"
    if secs < 480:
        return "blitz"
    if secs < 1500:
        return "rapid"
    return "classical"


def parse_game(pg: chess.pgn.Game, fallback_ts: int = 0) -> Game | None:
    h = pg.headers
    if _header(h, "Variant").lower() not in ("", "standard", "chess") or "FEN" in h or pg.errors:
        return None
    moves = [m.uci() for m in pg.mainline_moves()]
    if not moves:
        return None
    white, black = _header(h, "White"), _header(h, "Black")
    ident = "\n".join([*(h.get(k, "") for k in ("Event", "Site", "Date", "Round", "White", "Black")), *moves])
    site = _header(h, "Site")
    event = _header(h, "Event")
    return Game(
        source=SOURCE,
        id=hashlib.sha1(ident.encode()).hexdigest()[:16],
        url=site if site.startswith("http") else "",
        white=white,
        black=black,
        user_is_white=True,  # the side of the player followed is found when the games are used
        end_ts=_end_ts(h, fallback_ts),
        result=h.get("Result", "*"),
        moves=moves,
        time_class=time_class(h.get("TimeControl", "")),
        rated="rated" in event.lower(),
        white_elo=_header(h, "WhiteElo"),
        black_elo=_header(h, "BlackElo"),
        event=event,
    )


def _file_key(path: Path) -> str:
    return f"pgn_file:{path.resolve()}"


def fetch(
    cache,
    paths: Iterable[str],
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
    skip: str = "",
) -> int:
    """Store the games of the PGN files that changed since they were last read. Returns how many are new."""
    new = 0
    for path in files(paths, skip):
        try:
            st = path.stat()
        except OSError:
            continue
        sig = [st.st_mtime_ns, st.st_size]
        if cache.get(_file_key(path)) == sig:
            continue
        if progress:
            progress(f"PGN: reading {path.name}…")
        batch: list[Game] = []
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise http.Cancelled()
                pg = chess.pgn.read_game(f)
                if pg is None:
                    break
                g = parse_game(pg, int(st.st_mtime))
                if g is not None:
                    batch.append(g)
                if len(batch) >= 500:
                    new += cache.put_games(batch)
                    batch = []
        new += cache.put_games(batch)
        cache.set(_file_key(path), sig)
    return new

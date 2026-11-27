"""The stored games, written out as PGN: one file per account, and one for the games of the PGN inputs."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from pathlib import Path

import chess
import chess.pgn

from .config import SITES, Extract
from .model import Game

SITE_NAMES = {"lichess": "Lichess", "chesscom": "Chess.com", "pgn": "PGN"}


def game_pgn(g: Game) -> str:
    pg = chess.pgn.Game()
    h = pg.headers
    h["Event"] = g.event or f"{SITE_NAMES.get(g.source, g.source)} game"
    h["Site"] = g.url or "?"
    h["Date"] = time.strftime("%Y.%m.%d", time.gmtime(g.end_ts)) if g.end_ts else "????.??.??"
    h["White"] = g.white or "?"
    h["Black"] = g.black or "?"
    h["Result"] = g.result
    if g.white_elo:
        h["WhiteElo"] = g.white_elo
    if g.black_elo:
        h["BlackElo"] = g.black_elo
    node: chess.pgn.GameNode = pg
    board = chess.Board()
    for uci in g.moves:
        mv = chess.Move.from_uci(uci)
        if not board.is_legal(mv):
            break
        node = node.add_variation(mv)
        board.push(mv)
    return str(pg)


def write(path: Path, games: Iterable[Game]) -> int:
    """Write the games to `path` (replacing it in one step, so a reader never sees half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for g in games:
            f.write(game_pgn(g) + "\n\n")
            n += 1
    os.replace(tmp, path)
    return n


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "player"


def file_name(source: str, name: str) -> str:
    return f"{source}-{_safe(name)}.pgn"


INPUTS = "pgn-inputs.pgn"


def file_names(ex: Extract) -> list[str]:
    """The files `export_all` writes for these settings."""
    return [file_name(a.source, a.name) for a in ex.accounts] + ([INPUTS] if ex.pgn_paths else [])


def export_all(cache, ex: Extract, folder: str | Path) -> dict[str, int]:
    """Every account's stored games (all of them, whatever the look-back and filters), oldest first, and the
    games read from the PGN inputs. Returns file name → games written."""
    folder = Path(folder).expanduser()
    out: dict[str, int] = {}
    for a in ex.accounts:
        # a name in the PGN inputs is looked for in their games only
        games = cache.games(a.source if a.source in SITES else "pgn", a.name)
        out[file_name(a.source, a.name)] = write(folder / file_name(a.source, a.name), reversed(games))
    if ex.pgn_paths:
        out[INPUTS] = write(folder / INPUTS, reversed(cache.games_from("pgn")))
    return out

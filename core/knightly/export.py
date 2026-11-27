"""The stored games, written out as PGN: one file per account, and one for the games of the PGN inputs.

With `annotate.export_annotations`, each move the annotate stage covered gets its grade (?!, ?, ?? from how much
win rate it lost; the good grades need every move searched, so only puzzle cards have them) and the evaluation
after it, `[%eval 0.35]` or `[%eval #-3]` from White's side, as Lichess and ChessBase read it."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn

from .cache import fen_key
from .config import ME, SITES, Config, Extract
from .model import Game, Score
from .select import NEUTRAL, quick_nag

Notes = dict[int, tuple[int, str]]  # ply → (NAG or 0, evaluation after the move)

SITE_NAMES = {"lichess": "Lichess", "chesscom": "Chess.com", "pgn": "PGN"}


def game_pgn(g: Game, notes: Notes | None = None) -> str:
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
        nag, ev = (notes or {}).get(board.ply(), (0, ""))
        if nag:
            node.nags.add(nag)
        if ev:
            node.comment = f"[%eval {ev}]"
        board.push(mv)
    return str(pg)


@dataclass
class Grade:
    """One move of a game, as the annotate stage judged it."""

    mine: bool  # played by the player the game is followed for
    loss: float  # win rate lost against the best move (points)
    nag: int  # ?!, ?, ?? (0: none; the good grades need the full search)
    score: Score  # the evaluation after the move


def grades_of(g: Game, white: bool, have: dict, inaccuracy: float) -> dict[int, Grade]:
    """ply → grade of each move of `g` the annotate stage covered, the game followed for the player on White
    (`white`) or Black. `have`: the annotate stage's table (`cache.annotations()`)."""
    out: dict[int, Grade] = {}
    board = chess.Board()
    for uci in g.moves:
        mv = chess.Move.from_uci(uci)
        if not board.is_legal(mv):
            break
        turn = board.turn == chess.WHITE
        row = have.get((fen_key(board.fen()), board.ply(), turn == white))
        line = row[1]["lines"].get(uci) if row else None
        if line is not None:
            score = Score.from_json(line["score"])
            best = Score.from_json(row[1]["best"]["score"])
            loss = max(0.0, best.win_pct(turn) - score.win_pct(turn))
            nag = quick_nag(loss, inaccuracy)
            out[board.ply()] = Grade(turn == white, loss, 0 if nag == NEUTRAL else nag, score)
        board.push(mv)
    return out


def annotations_of(g: Game, white: bool, have: dict, inaccuracy: float) -> Notes:
    """The grade and evaluation of each move of `g` that the annotate stage covered (see `grades_of`)."""
    return {ply: (gr.nag, gr.score.pgn()) for ply, gr in grades_of(g, white, have, inaccuracy).items()}


def write(path: Path, games: Iterable[Game], notes: Callable[[Game], Notes | None] = lambda g: None) -> int:
    """Write the games to `path` (replacing it in one step, so a reader never sees half a file); `notes`: the
    annotations of a game."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for g in games:
            f.write(game_pgn(g, notes(g)) + "\n\n")
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


def export_all(cache, cfg: Config, folder: str | Path) -> dict[str, int]:
    """Every account's stored games (all of them, whatever the look-back and filters), oldest first, and the
    games read from the PGN inputs, annotated when the settings say so. Returns file name → games written."""
    from .stages.extract import side  # the stages import this module

    ex = cfg.extract
    folder = Path(folder).expanduser()
    have = cache.annotations() if cfg.annotate.export_annotations else {}

    def notes(g: Game) -> Notes | None:
        s = side(g, ex) if have else None
        if s is None or s[1] != ME:  # only your games are annotated
            return None
        return annotations_of(g, s[0], have, cfg.annotate.inaccuracy)

    out: dict[str, int] = {}
    for a in ex.accounts:
        # a name in the PGN inputs is looked for in their games only
        games = cache.games(a.source if a.source in SITES else "pgn", a.name)
        out[file_name(a.source, a.name)] = write(folder / file_name(a.source, a.name), reversed(games), notes)
    if ex.pgn_paths:
        out[INPUTS] = write(folder / INPUTS, reversed(cache.games_from("pgn")), notes)
    return out

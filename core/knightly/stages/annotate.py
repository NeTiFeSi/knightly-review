"""Stage 2, annotate: how good each move the games played was.

Input: the games (stage 1) as positions by ply (`stats.collect`). Output: for each position, the cheap scan
(`Analyser.scan`, at `cfg.annotate.scan_depth`): the best line and the line of each move the games played, from
which each played move's loss follows (how far its win rate falls below the best move's, engine WDL); and, for a
game that gets a puzzle, the grade (NAG) of its other moves and its scores. Searches are saved in the cache, so
another run, or another stage, gets them for free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import chess

from .. import stats
from ..config import Config
from ..model import Game, Line, Score
from ..progress import Tracker
from ..select import game_nags


@dataclass(eq=False)
class Unit:
    """One position at one ply, as seen from one side: the moves that side played from it."""

    pos: stats.Pos
    mine: bool
    board: chess.Board
    played: dict[str, stats.MoveStat]
    scan: tuple[Line, dict[str, Line]] | None = None  # the scan: the best line, and the line of each played move

    def losses(self) -> dict[str, float]:
        """Loss of each played move against the best move, from the scan."""
        if self.scan is None:
            return {}
        best, lines = self.scan
        white = self.board.turn == chess.WHITE
        top = best.score.win_pct(white)
        return {m: max(0.0, top - line.score.win_pct(white)) for m, line in lines.items()}

    def loss(self) -> float:
        """The loss of the worst move played here."""
        return max(self.losses().values(), default=0.0)


def _line(line: Line) -> dict:
    return {"score": line.score.to_json(), "moves": line.moves}


def _unline(d: dict) -> Line:
    return Line(Score.from_json(d["score"]), list(d["moves"]))


def scan_to_json(scan: tuple[Line, dict[str, Line]]) -> dict:
    best, lines = scan
    return {"best": _line(best), "lines": {m: _line(l) for m, l in lines.items()}}


def scan_from_json(d: dict) -> tuple[Line, dict[str, Line]]:
    return _unline(d["best"]), {m: _unline(l) for m, l in d["lines"].items()}


def annotated(row: tuple[int, dict] | None, u: Unit, depth: int) -> bool:
    """A saved annotation (depth, data) answers `u`: deep enough, and it has a line for every move played there
    (new games may have played others since)."""
    return row is not None and row[0] >= depth and set(u.played) <= set(row[1]["lines"])


def positions(by_ply: dict[int, dict[str, stats.Pos]], ply: int, mine: bool) -> list[Unit]:
    """The positions at `ply` where that side (yours, or your opponents') played, with a choice to make."""
    out = []
    for pos in by_ply.get(ply, {}).values():
        played = {u: st for u, st in pos.moves.items() if (st.mine if mine else st.opp)}
        if not played:
            continue
        board = chess.Board(pos.fen)
        if board.legal_moves.count() < 2:
            continue
        out.append(Unit(pos, mine, board, played))
    return out


class Annotator:
    def __init__(self, analyser, cfg: Config, tracker: Tracker):
        self.an, self.cfg, self.tracker = analyser, cfg, tracker
        self.analysed = 0  # positions scanned in this run
        self._graded: dict[str, dict[int, int]] = {}

    def scan(self, units: list[Unit], ply: int, tick: Callable[[], None]) -> None:
        """Scan each position (the tracker's current stage counts them); `tick` between positions."""
        for i, u in enumerate(units):
            tick()
            self.tracker.position(f"ply {ply}: position {i + 1} of {len(units)}")
            u.scan = self.an.scan(u.board, list(u.played))
            self.analysed += 1
            self.tracker.advance()

    def scores_of(self, game: Game) -> list[Score | None]:
        """The engine's score of each position of the game (White's view), from the searches in the cache: a
        position searched itself, else the score of the move played from the position before it."""
        boards = [chess.Board()]
        for m in game.moves:
            b = boards[-1].copy(stack=False)
            b.push_uci(m)
            boards.append(b)
        evs = [self.an.cached(b) for b in boards]
        out: list[Score | None] = []
        for i, ev in enumerate(evs):
            score = ev.lines[0].score if ev and ev.lines else None
            if score is None and i and evs[i - 1]:
                before = evs[i - 1]
                score = next((l.score for l in before.lines if l.moves and l.moves[0] == game.moves[i - 1]), None)
            out.append(score)
        return out

    def nags(self, game: Game, skip: set[int]) -> dict[int, int]:
        """The NAGs of a game's other moves, graded once per run (the analyser caches the searches)."""
        if game.key not in self._graded:
            label = f"Grading the moves of {game.white} – {game.black}"
            with self.tracker.sub("cards", label, total=len(game.moves), unit="positions", step="grading the moves"):
                self._graded[game.key] = game_nags(  # puzzle plies are graded in full already
                    game, self.an, self.cfg, skip, lambda i, n: self.tracker.advance() if i else None
                )
        return self._graded[game.key]

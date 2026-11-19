"""Shared test fixtures: a scripted game and an analyser that needs no engine."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

import chess

from knightly.cache import fen_key
from knightly.model import Eval, Game, Line, Score

FIXTURES = Path(__file__).parent / "fixtures"

def S(cp: int, w: int, d: int, l: int) -> Score:
    """A score with WDL (per mille, White's view)."""
    return Score(cp=cp, wdl=(w, d, l))


WHITE_MATES = Score(mate=1, wdl=(1000, 0, 0))

# 1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6?? 4. Qxf7#  — the user plays Black.
SCHOLAR_MOVES = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]
SCHOLAR_SCORES = [S(30, 100, 850, 50), S(30, 100, 850, 50), S(30, 100, 850, 50), S(-30, 50, 850, 100),
                  S(-20, 55, 850, 95), S(-30, 50, 850, 100), WHITE_MATES, Score(cp=10000, wdl=(1000, 0, 0))]


def scholar_game(**kw) -> Game:
    d = dict(
        source="lichess", id="test0001", url="https://lichess.org/test0001/black", white="Opponent",
        black="KnightlyMe", user_is_white=False, end_ts=1793000000, result="1-0", moves=list(SCHOLAR_MOVES),
        time_class="blitz", rated=True, white_elo="1500", black_elo="1480", event="Rated blitz game",
    )
    d.update(kw)
    return Game(**d)


def board_at(moves: list[str], ply: int) -> chess.Board:
    b = chess.Board()
    for u in moves[:ply]:
        b.push_uci(u)
    return b


PUZZLE_FEN = fen_key(board_at(SCHOLAR_MOVES, 5).fen())
# Black to move: g6, Qe7 and Qf6 are all within 2 % of the best; Nf6 allows mate.
PUZZLE_EVAL = Eval(depth=20, lines=[
    Line(S(-30, 50, 850, 100), ["g7g6", "h5f3"]),
    Line(S(-20, 55, 850, 95), ["d8e7", "b1c3"]),
    Line(S(-10, 70, 850, 80), ["d8f6", "b1c3"]),
    Line(WHITE_MATES, ["g8f6", "h5f7"]),
])


class FakeAnalyser:
    """Scripted evals instead of an engine; other positions are a dead draw with no line."""

    def __init__(self, cfg=None, cache=None, cancel=None, table: Optional[dict[str, Eval]] = None):
        self.table = {PUZZLE_FEN: PUZZLE_EVAL} if table is None else table
        self.stats = {"fake": 0}
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def evaluate(self, board, *, multipv=1, ply=99) -> Eval:
        self.calls += 1
        ev = self.table.get(fen_key(board.fen()))
        if ev:
            return Eval(ev.depth, ev.lines[:multipv], "fake")
        if board.is_checkmate():
            lost = (0, 0, 1000) if board.turn else (1000, 0, 0)
            return Eval(99, [Line(Score(cp=-10000 if board.turn else 10000, wdl=lost), [])], "fake")
        return Eval(10, [Line(S(0, 0, 1000, 0), [])], "fake")

    def analyse_game(self, game: Game, progress=None):
        return list(SCHOLAR_SCORES)


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}
        self.text = self._body.decode()

    def json(self):
        return json.loads(self._body)

    def iter_lines(self):
        return iter(io.BytesIO(self._body).read().splitlines())

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeSession:
    """Answers GETs from a list of (url-substring, response) rules, recording each request."""

    def __init__(self, rules):
        self.rules = rules
        self.requests = []
        self.headers = {}

    def get(self, url, **kw):
        self.requests.append((url, kw))
        for i, (frag, resp) in enumerate(self.rules):
            if frag in url:
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        return FakeResponse(404, b"{}")

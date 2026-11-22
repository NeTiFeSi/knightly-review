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
    Line(S(-10, 60, 850, 90), ["d8f6", "b1c3"]),
    Line(WHITE_MATES, ["g8f6", "h5f7"]),
])


def positions_scored(moves: list[str], scores: list[Score]) -> dict[str, Score]:
    """fen key → score, along a game."""
    out = {}
    b = chess.Board()
    for ply in range(len(moves) + 1):
        out[fen_key(b.fen())] = scores[ply]
        if ply < len(moves):
            b.push_uci(moves[ply])
    return out


class FakeAnalyser:
    """Scripted evals instead of an engine; other positions are a dead draw with no line."""

    def __init__(self, cfg=None, cache=None, cancel=None, table: Optional[dict[str, Eval]] = None):
        self.table = {PUZZLE_FEN: PUZZLE_EVAL} if table is None else table
        self.stats = {"fake": 0}
        self.calls = 0
        # Step-by-step scores (any depth): the scholar's mate game as scripted.
        self.scores = positions_scored(SCHOLAR_MOVES, SCHOLAR_SCORES)
        self.searched: list[tuple[str, int]] = []  # (fen key, depth) of each score() call

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

    def known(self, keys):
        return {}

    def score(self, board, depth):
        k = fen_key(board.fen())
        self.searched.append((k, depth))
        s = self.scores.get(k)
        return (s if s is not None else self.evaluate(board).lines[0].score), depth


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


# 1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 — the Najdorf.
SICILIAN = ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "a7a6"]
# 1. e4 e6 2. d4 d5
FRENCH = ["e2e4", "e7e6", "d2d4", "d7d5"]


def game_from(moves: list[str], *, white: bool, **kw) -> Game:
    d = dict(moves=list(moves), user_is_white=white, white="KnightlyMe" if white else "Opponent",
             black="Opponent" if white else "KnightlyMe", url="", result="1-0")
    d.update(kw)
    return scholar_game(**d)


def score_for(white_pct: float) -> Score:
    """A score whose expected score for White is `white_pct`."""
    if white_pct >= 50:
        w = round((white_pct - 50) * 20)
        return S(0, w, 1000 - w, 0)
    l = round((50 - white_pct) * 20)
    return S(0, 0, 1000 - l, l)


def scores_with(losses: dict[int, float], n_moves: int) -> list[Score]:
    """Quick-pass scores where the move at each ply in `losses` loses that much for its side."""
    e = 50.0
    out = [score_for(e)]
    for ply in range(n_moves):
        loss = losses.get(ply, 0)
        e += -loss if ply % 2 == 0 else loss
        out.append(score_for(e))
    return out


class ScriptedAnalyser(FakeAnalyser):
    """Quick pass from `script` (game id → {ply: loss}); the deep pass agrees with it.

    At each scripted position another legal move holds the balance and the played
    move loses what the script says.
    """

    def __init__(self, games: list[Game], script: dict[str, dict[int, float]]):
        super().__init__(table={})
        self.games = {g.id: g for g in games}
        self.script = script
        self.scores = {}
        for g in games:
            e = 50.0
            for ply, u in enumerate(g.moves):
                b = board_at(g.moves, ply)
                loss = script.get(g.id, {}).get(ply, 0)
                after = e - loss if ply % 2 == 0 else e + loss
                if loss:
                    alt = next(m.uci() for m in b.legal_moves if m.uci() != u)
                    self.table[fen_key(b.fen())] = Eval(20, [Line(score_for(e), [alt]), Line(score_for(after), [u])])
                e = after
            self.scores.update(positions_scored(g.moves, scores_with(script.get(g.id, {}), len(g.moves))))

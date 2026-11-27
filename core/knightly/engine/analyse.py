"""Position evaluation with Stockfish, always with win/draw/loss (UCI_ShowWDL).

Every position is searched the same way: to the fixed depth `cfg.sf_depth`, with one line per legal move
(MultiPV = the number of legal moves). With a single line, Stockfish prunes the moves it thinks are worse and
only bounds their scores; searching them all gives each move an exact score from the same search, so the loss
of a move against the best one is comparable. Results are cached by position and count when they were searched
at least as deep as asked.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Self

import chess
import chess.engine

from ..cache import Cache, fen_key
from ..config import Config
from ..http import Cancelled
from ..model import Eval, Line, NoWdl, Score
from ..progress import STOCKFISH as P_SF
from ..progress import Tracker
from .discover import is_stockfish, popen_kwargs

log = logging.getLogger(__name__)

WDL_OPTION = "UCI_ShowWDL"
MATED = 10000  # |cp| used for a checkmated position (the side to move lost)


class EngineError(RuntimeError):
    pass


def _score(info: chess.engine.InfoDict) -> Score:
    pov = info["score"].white()
    if "wdl" not in info:
        raise NoWdl("the engine sent a score without WDL")
    w = info["wdl"].white()
    wdl = (w.wins, w.draws, w.losses)
    if pov.is_mate():
        return Score(mate=pov.mate(), wdl=wdl)
    return Score(cp=pov.score(), wdl=wdl)


def terminal(board: chess.Board) -> Score:
    if board.is_checkmate():
        white_lost = board.turn == chess.WHITE
        return Score(cp=-MATED if white_lost else MATED, wdl=(0, 0, 1000) if white_lost else (1000, 0, 0))
    return Score(cp=0, wdl=(0, 1000, 0))


class Engine:
    """One Stockfish process."""

    def __init__(
        self,
        path: str,
        cfg: Config,
        cache: Cache,
        tracker: Tracker | None = None,
        cancel: threading.Event | None = None,
    ):
        self.path, self.cfg, self.cache = path, cfg, cache
        self._proc: chess.engine.SimpleEngine | None = None
        self.name = ""
        self.calls = 0
        self.tracker = tracker  # told about every search (progress.py)
        self.cancel = cancel  # a search in progress stops when this is set

    def proc(self) -> chess.engine.SimpleEngine:
        if self._proc is None:
            if not self.path:
                raise EngineError("no Stockfish configured")
            eng = chess.engine.SimpleEngine.popen_uci(self.path, **popen_kwargs())
            name = eng.id.get("name", "")
            if not is_stockfish(name):
                eng.quit()
                raise EngineError(f"{self.path} is {name or 'not a known engine'}, not Stockfish")
            if WDL_OPTION not in eng.options:
                eng.quit()
                raise EngineError(f"{name} doesn't report win/draw/loss ({WDL_OPTION}); please update it")
            conf = {WDL_OPTION: True, "Threads": self.cfg.engine_threads(), "Hash": self.cfg.hash_mb}
            eng.configure({k: v for k, v in conf.items() if k in eng.options})
            self._proc, self.name = eng, name
            if self.tracker:
                self.tracker.engine(P_SF, name=name, enabled=True)
        return self._proc

    def _search(self, board: chess.Board, depth: int, lines: int, t0: float) -> list:
        """Every legal move's line, streaming progress to the tracker; stops at once on cancel."""
        with self.proc().analysis(board, chess.engine.Limit(depth=depth), multipv=lines) as a:
            for info in a:
                if self.cancel is not None and self.cancel.is_set():
                    a.stop()
                    raise Cancelled()
                if self.tracker and "depth" in info:
                    self.tracker.search_progress(
                        P_SF,
                        depth=int(info.get("depth", 0)),
                        nodes=int(info.get("nodes", 0)),
                        nps=int(info.get("nps", 0)),
                        ms=round(info.get("time", time.monotonic() - t0) * 1000),
                    )
            return [dict(i) for i in a.multipv]

    def evaluate(self, board: chess.Board) -> Eval:
        """`board` searched to `cfg.sf_depth` with one line per legal move, best first (from the cache when the
        position was searched that deep already)."""
        key = fen_key(board.fen())
        depth = self.cfg.sf_depth
        ev = self.cache.get_eval(key, min_effort=depth)
        if ev:
            if self.tracker:
                self.tracker.search_end(P_SF, cached=True)
            return ev
        lines = board.legal_moves.count()
        if self.tracker:
            self.tracker.search_start(P_SF, board.fen(), board.ply(), lines, depth)
        t0 = time.monotonic()
        try:
            infos = self._search(board, depth, lines, t0)
        except BaseException:
            if self.tracker:
                self.tracker.search_end(P_SF, ms=round((time.monotonic() - t0) * 1000))
            raise
        if self.tracker:
            self.tracker.search_end(P_SF, ms=max(1, round((time.monotonic() - t0) * 1000)))
        result = [Line(_score(i), [m.uci() for m in i.get("pv", [])]) for i in infos if "score" in i]
        ev = Eval(depth=int(min(i.get("depth", 0) for i in infos)), lines=result, source="stockfish")
        self.calls += 1
        self.cache.put_eval(key, depth, ev)
        return ev

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.quit()
            except Exception:
                log.debug("engine did not quit cleanly", exc_info=True)
            self._proc = None


class Analyser:
    """The engine, as the pipeline uses it. Not thread-safe: use one per worker thread."""

    def __init__(
        self, cfg: Config, cache: Cache, cancel: threading.Event | None = None, tracker: Tracker | None = None
    ):
        self.cfg = cfg
        self.cache = cache
        self.cancel = cancel or threading.Event()
        self.tracker = tracker
        self.sf = Engine(cfg.stockfish_path, cfg, cache, tracker, self.cancel)
        if tracker:
            tracker.engine(P_SF, enabled=True)

    @property
    def stats(self) -> dict:
        return {"stockfish_runs": self.sf.calls}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.sf.close()

    def _check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def evaluate(self, board: chess.Board) -> Eval:
        """Every legal move of `board` with its score and line, best first. A finished game has one empty line."""
        self._check()
        if board.is_game_over():
            return Eval(depth=99, lines=[Line(terminal(board), [])], source="rules")
        return self.sf.evaluate(board)

    def cached(self, board: chess.Board) -> Eval | None:
        """The position's search when it is in the cache already (never starts one)."""
        return self.cache.get_eval(fen_key(board.fen()), min_effort=self.cfg.sf_depth)

"""Position evaluation with Stockfish (required) and Lc0 (optional), always with WDL.

- Stockfish is searched to a fixed depth (`cfg.sf_depth`). Both engines must report
  win/draw/loss (UCI_ShowWDL).
- With Lc0 on, Lc0 runs after Stockfish, for as long as Stockfish took on that
  position (stored with Stockfish's cached result). The answer comes from Lc0, except
  in mate positions (either engine's best line is a mate), where Stockfish's is used.
- Results are cached per engine and position; a cached result counts when it was
  searched at least as hard (Stockfish: depth; Lc0: milliseconds).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from typing import Self

import chess
import chess.engine

from ..cache import Cache, fen_key
from ..config import Config
from ..http import Cancelled
from ..model import Eval, Line, NoWdl, Score
from ..progress import LC0 as P_LC0
from ..progress import STOCKFISH as P_SF
from ..progress import Tracker
from .discover import LC0, STOCKFISH, kind_of, popen_kwargs

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


def is_mate(ev: Eval | None) -> bool:
    return bool(ev and ev.lines and ev.lines[0].score.mate is not None)


class Engine:
    """One engine process of a known kind, with its own search limit and cache entries."""

    def __init__(
        self,
        kind: str,
        path: str,
        cfg: Config,
        cache: Cache,
        tracker: Tracker | None = None,
        cancel: threading.Event | None = None,
    ):
        self.kind, self.path, self.cfg, self.cache = kind, path, cfg, cache
        self._proc: chess.engine.SimpleEngine | None = None
        self.name = ""
        self.calls = 0
        self.tracker = tracker  # told about every search (progress.py)
        self.cancel = cancel  # a search in progress stops when this is set

    def limit(self, depth: int | None = None, ms: int | None = None) -> tuple[chess.engine.Limit, int]:
        """The search limit and the cache effort it stands for: Stockfish by depth (`cfg.sf_depth`
        unless `depth`), Lc0 by time (`ms`, required)."""
        if self.kind == STOCKFISH:
            d = depth or self.cfg.sf_depth
            return chess.engine.Limit(depth=d), d
        if not ms:
            raise ValueError("Lc0 needs a time limit")
        return chess.engine.Limit(time=ms / 1000), ms

    def proc(self) -> chess.engine.SimpleEngine:
        if self._proc is None:
            label = "Stockfish" if self.kind == STOCKFISH else "Lc0"
            if not self.path:
                raise EngineError(f"no {label} configured")
            eng = chess.engine.SimpleEngine.popen_uci(self.path, **popen_kwargs())
            name = eng.id.get("name", "")
            if kind_of(name) != self.kind:
                eng.quit()
                raise EngineError(f"{self.path} is {name or 'not a known engine'}, not {label}")
            if WDL_OPTION not in eng.options:
                eng.quit()
                raise EngineError(f"{name} doesn't report win/draw/loss ({WDL_OPTION}); please update it")
            conf: dict = {WDL_OPTION: True}
            if self.kind == STOCKFISH:
                conf["Threads"] = self.cfg.engine_threads()
                conf["Hash"] = self.cfg.hash_mb
            elif self.cfg.lc0_weights:
                conf["WeightsFile"] = self.cfg.lc0_weights
            eng.configure({k: v for k, v in conf.items() if k in eng.options})
            self._proc, self.name = eng, name
            if self.tracker:
                self.tracker.engine(P_SF if self.kind == STOCKFISH else P_LC0, name=name, enabled=True)
        return self._proc

    def _search(
        self, board: chess.Board, limit: chess.engine.Limit, multipv: int, root: list[chess.Move] | None, t0: float
    ) -> list:
        """The engine's best line(s), streaming its progress to the tracker; stops at once on cancel."""
        kind = P_SF if self.kind == STOCKFISH else P_LC0
        with self.proc().analysis(board, limit, multipv=multipv, root_moves=root) as a:
            for info in a:
                if self.cancel is not None and self.cancel.is_set():
                    a.stop()
                    raise Cancelled()
                if self.tracker and "depth" in info:
                    self.tracker.search_progress(
                        kind,
                        depth=int(info.get("depth", 0)),
                        nodes=int(info.get("nodes", 0)),
                        nps=int(info.get("nps", 0)),
                        ms=round(info.get("time", time.monotonic() - t0) * 1000),
                    )
            return [dict(i) for i in a.multipv]

    def evaluate(
        self,
        board: chess.Board,
        multipv: int,
        moves: list[str] | None = None,
        depth: int | None = None,
        ms: int | None = None,
        timed: bool = False,
    ) -> Eval:
        """With `moves`, only those root moves are searched (not cached). `depth` (Stockfish)
        searches to that depth instead of the configured one; `ms` is Lc0's time. `timed`: a
        cached result must know how long it took (`Eval.ms`), or the search is run again."""
        key = fen_key(board.fen())
        limit, effort = self.limit(depth, ms)
        kind = P_SF if self.kind == STOCKFISH else P_LC0
        if not moves:
            ev = self.cache.get_eval(key, self.kind, multipv=multipv, min_effort=effort)
            if ev and (ev.ms or not timed):
                if self.tracker:
                    self.tracker.search_end(kind, cached=True)
                return ev
        root = [chess.Move.from_uci(m) for m in moves] if moves else None
        if self.tracker:
            self.tracker.search_start(kind, board.fen(), board.ply(), multipv, effort)
        t0 = time.monotonic()
        try:
            infos = self._search(board, limit, multipv, root, t0)
        except BaseException:
            if self.tracker:
                self.tracker.search_end(kind, ms=round((time.monotonic() - t0) * 1000))
            raise
        took = max(1, round((time.monotonic() - t0) * 1000))
        if self.tracker:
            self.tracker.search_end(kind, ms=took)
        lines = [Line(_score(i), [m.uci() for m in i.get("pv", [])]) for i in infos if "score" in i]
        ev = Eval(depth=int(min(i.get("depth", 0) for i in infos)), lines=lines, source=self.kind, ms=took)
        self.calls += 1
        if not moves:
            self.cache.put_eval(key, self.kind, effort, ev)
        return ev

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.quit()
            except Exception:
                log.debug("engine did not quit cleanly", exc_info=True)
            self._proc = None


class Analyser:
    """Stockfish, plus Lc0 when enabled. Not thread-safe: use one per worker thread."""

    def __init__(
        self, cfg: Config, cache: Cache, cancel: threading.Event | None = None, tracker: Tracker | None = None
    ):
        self.cfg = cfg
        self.cancel = cancel or threading.Event()
        self.tracker = tracker
        self.sf = Engine(STOCKFISH, cfg.stockfish_path, cfg, cache, tracker, self.cancel)
        self.lc0 = Engine(LC0, cfg.lc0_path, cfg, cache, tracker, self.cancel) if cfg.use_lc0 and cfg.lc0_path else None
        if tracker:
            tracker.engine(P_SF, enabled=True)
            tracker.engine(P_LC0, enabled=self.lc0 is not None)
        self.picked = {STOCKFISH: 0, LC0: 0}

    @property
    def stats(self) -> dict:
        s = {"stockfish_runs": self.sf.calls, "used_stockfish": self.picked[STOCKFISH]}
        if self.lc0:
            s.update(lc0_runs=self.lc0.calls, used_lc0=self.picked[LC0])
        return s

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.sf.close()
        if self.lc0:
            self.lc0.close()

    def _check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def evaluate(
        self,
        board: chess.Board,
        *,
        multipv: int = 1,
        ply: int = 99,
        moves: list[str] | None = None,
        sf_only: bool = False,
    ) -> Eval:
        """Eval of `board` with up to `multipv` lines (fewer if there are fewer legal moves).

        With `moves`, only those root moves are searched. `sf_only`: Stockfish alone, even with Lc0 on
        (mate puzzles: Stockfish is the authority on mates).
        """
        self._check()
        if board.is_game_over():
            return Eval(depth=99, lines=[Line(terminal(board), [])], source="rules")
        n = min(multipv, len(moves) if moves else board.legal_moves.count())
        if self.lc0 is None or sf_only:
            ev = self.sf.evaluate(board, n, moves)
            self.picked[STOCKFISH] += 1
            return ev
        sf = self.sf.evaluate(board, n, moves, timed=True)
        self._check()
        lc = self.lc0.evaluate(board, n, moves, ms=sf.ms)  # as long as Stockfish took
        if is_mate(sf) or is_mate(lc):  # Stockfish is the authority on mates
            self.picked[STOCKFISH] += 1
            return sf
        self.picked[LC0] += 1
        return lc

    def score(self, board: chess.Board, depth: int) -> tuple[Score, int]:
        """Stockfish alone, one line, at `depth` (or a deeper cached result): the score
        (White's view, with WDL) and the depth it was searched to. Not for finished games."""
        self._check()
        ev = self.sf.evaluate(board, 1, depth=depth)
        if not ev.lines:
            return terminal(board), depth
        return ev.lines[0].score, max(depth, ev.depth)

    def known(self, keys: Iterable[str]) -> dict[str, tuple[int, Score]]:
        """Deepest cached Stockfish score of each position: fen key → (depth, score)."""
        out = {}
        for k, (effort, ev) in self.sf.cache.best_evals(list(keys), STOCKFISH).items():
            if ev.lines:
                out[k] = (effort, ev.lines[0].score)
        return out

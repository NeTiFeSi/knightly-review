"""Position evaluation with Stockfish, always with win/draw/loss (UCI_ShowWDL).

Two kinds of search, each to a fixed depth:

- the **full search** (`evaluate`, depth `cfg.sf_depth`): one line per legal move (MultiPV = the number of legal
  moves). With a single line, Stockfish prunes the moves it thinks are worse and only bounds their scores;
  searching them all gives each move an exact score from the same search, so the loss of a move against the best
  one is comparable.
- the **scan** (`scan`, depth `cfg.scan_depth_used()`): a cheap first look with the fewest lines that can
  measure a loss: one line for the best move, and each move the games played searched on its own (Stockfish's
  `searchmoves`), so its score is exact too. It tells which positions are worth a full search.

Results are cached by position and count when they were searched at least as deep as asked; a cached full search
answers a scan for free.
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

    def _search(
        self, board: chess.Board, depth: int, lines: int, t0: float, root: list[chess.Move] | None = None
    ) -> list:
        """`lines` lines (of the `root` moves only, when given), streaming progress to the tracker; stops at once
        on cancel."""
        with self.proc().analysis(board, chess.engine.Limit(depth=depth), multipv=lines, root_moves=root) as a:
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

    def scan(self, board: chess.Board, moves: list[str]) -> tuple[Line, dict[str, Line]]:
        """The cheap look at `board`: (the best line, the line of each of `moves`) at `cfg.scan_depth_used()`.

        One search with one line finds the best move; the other moves are searched together with only those as
        root moves (one line each). Whatever the cache has already (a full search counts) is not searched again."""
        key = fen_key(board.fen())
        full = self.cache.get_eval(key, min_effort=self.cfg.sf_depth)
        if full and full.lines:
            by = {l.moves[0]: l for l in full.lines if l.moves}
            return full.lines[0], {m: by[m] for m in moves if m in by}
        depth = self.cfg.scan_depth_used()
        have = {m: ev.lines[0] for m, ev in self.cache.get_scan(key, ["", *moves], depth).items() if ev.lines}
        saved: dict[str, Eval] = {}
        if "" not in have:
            best = self._scan_search(board, depth, 1, None)[0]
            have[""] = best
            saved[""] = Eval(depth, [best], "stockfish")
        best = have[""]
        best_move = best.moves[0] if best.moves else ""
        need = [m for m in dict.fromkeys(moves) if m != best_move and m not in have]
        if need:
            for line in self._scan_search(board, depth, len(need), need):
                if line.moves:
                    have[line.moves[0]] = line
                    saved[line.moves[0]] = Eval(depth, [line], "stockfish")
        if saved:
            self.cache.put_scan(key, depth, saved)
        return best, {m: (best if m == best_move else have[m]) for m in moves if m == best_move or m in have}

    def top(self, board: chess.Board, lines: int) -> Eval:
        """The `lines` best lines of `board`, best first, at the scan depth (cached; a full search answers it)."""
        key = fen_key(board.fen())
        n = min(lines, board.legal_moves.count())
        full = self.cache.get_eval(key, min_effort=self.cfg.sf_depth)
        if full and full.lines:
            return Eval(full.depth, full.lines[:n], full.source)
        depth, name = self.cfg.scan_depth_used(), f"*{n}"
        have = self.cache.get_scan(key, [name], depth)
        if name in have:
            return have[name]
        ev = Eval(depth, self._scan_search(board, depth, n, None), "stockfish")
        self.cache.put_scan(key, depth, {name: ev})
        return ev

    def _scan_search(self, board: chess.Board, depth: int, lines: int, only: list[str] | None) -> list[Line]:
        if self.tracker:
            self.tracker.search_start(P_SF, board.fen(), board.ply(), lines, depth)
        t0 = time.monotonic()
        try:
            infos = self._search(board, depth, lines, t0, [chess.Move.from_uci(m) for m in only] if only else None)
        except BaseException:
            if self.tracker:
                self.tracker.search_end(P_SF, ms=round((time.monotonic() - t0) * 1000))
            raise
        if self.tracker:
            self.tracker.search_end(P_SF, ms=max(1, round((time.monotonic() - t0) * 1000)))
        self.calls += 1
        return [Line(_score(i), [m.uci() for m in i.get("pv", [])]) for i in infos if "score" in i]

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

    def scan(self, board: chess.Board, moves: list[str]) -> tuple[Line, dict[str, Line]]:
        """The cheap first look: (the best line, the line of each of `moves`), see `Engine.scan`. A finished game
        has nothing to compare: its one empty line, and no move."""
        self._check()
        if board.is_game_over():
            line = Line(terminal(board), [])
            return line, {}
        return self.sf.scan(board, moves)

    def top(self, board: chess.Board, lines: int) -> Eval:
        """The `lines` best lines at the scan depth (see `Engine.top`); a finished game has one empty line."""
        self._check()
        if board.is_game_over():
            return Eval(depth=99, lines=[Line(terminal(board), [])], source="rules")
        return self.sf.top(board, lines)

    def cached(self, board: chess.Board) -> Eval | None:
        """The position's search when it is in the cache already (never starts one)."""
        return self.cache.get_eval(fen_key(board.fen()), min_effort=self.cfg.sf_depth)

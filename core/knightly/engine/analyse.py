"""Position evaluation with Stockfish (required) and Lc0 (optional), always with WDL.

- Stockfish is searched to a fixed depth (`cfg.sf_depth`), Lc0 for a fixed time
  (`cfg.lc0_time`). Both must report win/draw/loss (UCI_ShowWDL).
- With Lc0 on, both engines analyse each position at the same time. The answer
  comes from Lc0, except in mate positions (either engine's best line is a mate),
  where Stockfish's answer is used.
- Results are cached per engine and position; a cached result counts when it was
  searched at least as hard (Stockfish: depth; Lc0: milliseconds).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import chess
import chess.engine

from ..cache import Cache, fen_key
from ..config import Config
from ..http import Cancelled
from ..model import Eval, Game, Line, NoWdl, Score
from .discover import LC0, STOCKFISH, kind_of, popen_kwargs

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


def is_mate(ev: Optional[Eval]) -> bool:
    return bool(ev and ev.lines and ev.lines[0].score.mate is not None)


class Engine:
    """One engine process of a known kind, with its own search limit and cache entries."""

    def __init__(self, kind: str, path: str, cfg: Config, cache: Cache):
        self.kind, self.path, self.cfg, self.cache = kind, path, cfg, cache
        self._proc: Optional[chess.engine.SimpleEngine] = None
        self.name = ""
        self.calls = 0

    @property
    def effort(self) -> int:
        return self.cfg.sf_depth if self.kind == STOCKFISH else int(round(self.cfg.lc0_time * 1000))

    def limit(self) -> chess.engine.Limit:
        if self.kind == STOCKFISH:
            return chess.engine.Limit(depth=self.cfg.sf_depth)
        return chess.engine.Limit(time=self.cfg.lc0_time)

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
        return self._proc

    def evaluate(self, board: chess.Board, multipv: int, moves: Optional[list[str]] = None) -> Eval:
        """With `moves`, only those root moves are searched (not cached)."""
        key = fen_key(board.fen())
        if not moves:
            ev = self.cache.get_eval(key, self.kind, multipv=multipv, min_effort=self.effort)
            if ev:
                return ev
        root = [chess.Move.from_uci(m) for m in moves] if moves else None
        infos = self.proc().analyse(board, self.limit(), multipv=multipv, root_moves=root)
        if isinstance(infos, dict):
            infos = [infos]
        lines = [Line(_score(i), [m.uci() for m in i.get("pv", [])]) for i in infos if "score" in i]
        ev = Eval(depth=int(min(i.get("depth", 0) for i in infos)), lines=lines, source=self.kind)
        self.calls += 1
        if not moves:
            self.cache.put_eval(key, self.kind, self.effort, ev)
        return ev

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.quit()
            except Exception:
                pass
            self._proc = None


class Analyser:
    """Stockfish, plus Lc0 when enabled. Not thread-safe: use one per worker thread."""

    def __init__(self, cfg: Config, cache: Cache, cancel: Optional[threading.Event] = None,
                 gate: Optional[threading.Event] = None):
        self.cfg = cfg
        self.cancel = cancel or threading.Event()
        # Cleared while something more urgent (live play during review) needs the CPU.
        self.gate = gate
        self.sf = Engine(STOCKFISH, cfg.stockfish_path, cfg, cache)
        self.lc0 = Engine(LC0, cfg.lc0_path, cfg, cache) if cfg.use_lc0 and cfg.lc0_path else None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="knightly-engine") if self.lc0 else None
        self.picked = {STOCKFISH: 0, LC0: 0}

    @property
    def stats(self) -> dict:
        s = {"stockfish_runs": self.sf.calls, "used_stockfish": self.picked[STOCKFISH]}
        if self.lc0:
            s.update(lc0_runs=self.lc0.calls, used_lc0=self.picked[LC0])
        return s

    def __enter__(self) -> "Analyser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.sf.close()
        if self.lc0:
            self.lc0.close()
        if self._pool:
            self._pool.shutdown(wait=True)

    def _check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        while self.gate is not None and not self.gate.wait(0.25):
            if self.cancel.is_set():
                raise Cancelled()

    def evaluate(self, board: chess.Board, *, multipv: int = 1, ply: int = 99,
                 moves: Optional[list[str]] = None) -> Eval:
        """Eval of `board` with up to `multipv` lines (fewer if there are fewer legal moves).

        With `moves`, only those root moves are searched.
        """
        self._check()
        if board.is_game_over():
            return Eval(depth=99, lines=[Line(terminal(board), [])], source="rules")
        n = min(multipv, len(moves) if moves else board.legal_moves.count())
        if self.lc0 is None or self._pool is None:
            ev = self.sf.evaluate(board, n, moves)
            self.picked[STOCKFISH] += 1
            return ev
        f_sf = self._pool.submit(self.sf.evaluate, board.copy(stack=False), n, moves)
        f_lc = self._pool.submit(self.lc0.evaluate, board.copy(stack=False), n, moves)
        sf, lc = f_sf.result(), f_lc.result()
        if is_mate(sf) or is_mate(lc):  # Stockfish is the authority on mates
            self.picked[STOCKFISH] += 1
            return sf
        self.picked[LC0] += 1
        return lc

    def analyse_game(self, game: Game, progress: Optional[Callable[[int, int], None]] = None) -> list[Optional[Score]]:
        """Score (White's view, with WDL) of every position in the game, index 0 = start position."""
        board = chess.Board()
        positions = [board.copy(stack=False)]
        for u in game.moves:
            board.push_uci(u)
            positions.append(board.copy(stack=False))
        scores: list[Optional[Score]] = []
        for i, b in enumerate(positions):
            scores.append(self.evaluate(b, ply=i).score)
            if progress:
                progress(i + 1, len(positions))
        return scores

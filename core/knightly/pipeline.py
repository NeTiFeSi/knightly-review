"""The one way puzzles are made: ply by ply, the biggest suspected mistakes analysed deepest first.

For ply 0, 1, 2… (half-moves already played), the user's positions first and then, with
`cfg.include_opponent`, the opponents':

1. Every legal move of every position at that ply is searched at a low depth (`scan_steps`), and
   each position gets the loss of the worst move a player actually played there: how far its
   win rate falls below the best move's (engine WDL).
2. The position with the biggest loss is promoted: its played moves and its most promising
   alternatives are searched at the next depth, and it goes back in line with its new loss.
   A position whose loss drops under the limit is eliminated.
3. A position that has climbed every step gets the full setup (`select.build_card`, the configured
   depth, every move graded). If the played move is not a good move, it is a puzzle: one note with
   two puzzles, the position before the mistake and the position after it (the punishment).
4. Then the biggest loss among the rest is promoted, until none is left; then the next ply.

A puzzle very similar to one already made is not a note of its own: the old puzzle's note gets a
comment for each correct answer instead (`similar.py`). How often each move was played in a
position (`stats.py`) goes in the note's Notes field and is refreshed when the games change.

Every search is cached by position and depth, so a run carries on where the last one stopped.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import chess

from . import notes as notes_mod, similar, stats
from .cache import fen_key
from .config import Config
from .engine.analyse import terminal
from .follow import follow_puzzle
from .progress import Tracker
from .model import CardSpec, Game, NoteSpec, Score
from .puzzles import Puzzle, Store, puzzle_id
from .select import ACCEPTED, build_card, build_punish, game_nags, grade_position

DONE = 10_000  # depth given to finished positions (mate, stalemate…): nothing to search
UNBOUNDED = 10**6  # `want` at or above this: no cap, keep confirming (pre-calculating)
EXTRA_LINES = 3  # at each step after the first, the best moves by current score searched besides the played ones
CONFIRM_TRIES = 3  # worst moves of a position tried at full strength before giving up on it

Progress = Callable[[str, int, int, str], None]


def depth_steps(full: int) -> list[int]:
    """The depths a position climbs, ending at the configured one: two or three steps (13 → 5, 9, 13)."""
    full = max(1, full)
    return sorted({min(full, max(4, round(full * 0.4))), min(full, max(4, round(full * 0.7))), full})


def scan_steps(full: int) -> list[int]:
    """The depths before the full setup: all of `depth_steps` but the last (the last alone when it is the only one)."""
    steps = depth_steps(full)
    return steps[:-1] or steps


class _Stop(Exception):
    """The time is up or enough puzzles are made."""


@dataclass(eq=False)
class Unit:
    """One position at one ply, as seen from one side: the moves that side played from it."""

    pos: stats.Pos
    mine: bool
    board: chess.Board
    played: dict[str, stats.MoveStat]
    child: dict[str, str] = field(default_factory=dict)  # legal move → full FEN after it
    level: int = 0


class Miner:
    def __init__(self, games: Iterable[Game], analyser, cfg: Config, store: Store, *, want: int,
                 known: Iterable[Puzzle] = (), legacy: Iterable[str] = (), existing: Optional[dict[str, str]] = None,
                 make: bool = True, tracker: Optional[Tracker] = None,
                 on_note: Optional[Callable[[NoteSpec], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        """`games` newest first. `known`: the puzzles already in the collection; `legacy`: fen keys of
        positions on other cards (never offered again); `existing`: note key → PGN of the collection's notes.
        `make=False` (pre-calculating): analyse and warm the cache, make and change no notes."""
        self.games = list(games)
        self.by_key = {g.key: g for g in self.games}
        self.an, self.cfg, self.store, self.want, self.make = analyser, cfg, store, want, make
        self.legacy = set(legacy)
        self.existing = existing or {}
        self.tracker = tracker or Tracker()
        self.on_note = on_note or (lambda spec: None)
        self.clock = clock
        self.deadline = clock() + cfg.analysis_minutes * 60 if cfg.analysis_minutes > 0 else math.inf
        self.known: dict[str, Puzzle] = {p.id: p for p in known}
        self.merged = {bid for p in self.known.values() for bid in p.links}
        self.by_ply = stats.collect(self.games)
        self.scan = scan_steps(cfg.sf_depth)
        self.sc: dict[str, tuple[Score, int]] = {}  # fen key → (score, depth), White's view
        self._graded: dict[str, dict[int, int]] = {}
        self.notes: list[NoteSpec] = []
        self.made: set[str] = set()  # keys of the notes made in this run
        self.found = 0  # puzzles confirmed (made, or only found when `make` is off)
        self.analysed = 0  # positions searched by the engine in this run
        self.finished = False  # nothing left to continue: the run ended by itself, not out of time
        self._step = ""

    # --- helpers
    def out_of_time(self) -> bool:
        return self.clock() >= self.deadline

    def _tick(self) -> None:
        if self.out_of_time():
            raise _Stop()

    def _msg(self, step: str) -> str:
        """The stage's message: the step, the puzzles found so far and the time left."""
        left = self.deadline - self.clock()
        budget = "" if left == math.inf else f" · {max(0, math.ceil(left / 60))} min left"
        if self.want >= UNBOUNDED:
            return f"{step} ({self.found} puzzles found{budget})"
        return f"{step} ({self.found} of {self.want} puzzles found{budget})"

    def _say(self, step: str, detail: str = "", position: Optional[str] = None) -> None:
        """Change the message of the current stage (a forced update when it differs)."""
        self.tracker.budget(None if self.deadline == math.inf else max(0.0, (self.deadline - self.clock()) / 60))
        if position is not None:
            self.tracker.position(position)
        self.tracker.message(self._msg(step), step=detail, force=step != self._step)
        self._step = step

    def _score(self, fen: str, depth: int) -> None:
        k = fen_key(fen)
        cur = self.sc.get(k)
        if cur and cur[1] >= depth:
            self.tracker.advance()
            return
        self._tick()
        board = chess.Board(fen)
        if board.is_game_over():
            self.sc[k] = (terminal(board), DONE)
        else:
            s, d = self.an.score(board, depth)
            self.analysed += 1
            self.sc[k] = (s, d)
        self.tracker.advance()

    def _pct(self, u: Unit, move: str) -> Optional[float]:
        cur = self.sc.get(fen_key(u.child[move]))
        return None if cur is None else cur[0].win_pct(u.board.turn == chess.WHITE)

    def _losses(self, u: Unit) -> dict[str, float]:
        """Loss of each played move that has a score, against the best scored move."""
        pcts = {m: p for m in u.child if (p := self._pct(u, m)) is not None}
        if not pcts:
            return {}
        best = max(pcts.values())
        return {m: best - pcts[m] for m in u.played if m in pcts}

    def loss(self, u: Unit) -> float:
        return max(self._losses(u).values(), default=0.0)

    def _search(self, u: Unit, level: int) -> None:
        """Search the unit's moves at the depth of `level`: all of them first, then the played ones and the
        best by current score."""
        depth = self.scan[level]
        if level == 0:
            moves = list(u.child)
        else:
            by_score = sorted((m for m in u.child if self._pct(u, m) is not None), key=lambda m: -(self._pct(u, m) or 0))
            moves = list(dict.fromkeys([*u.played, *by_score[:EXTRA_LINES]]))
        if level:
            self.tracker.plan(len(moves))
        for m in moves:
            self._score(u.child[m], depth)

    # --- the loop
    def run(self) -> None:
        try:
            self.refresh_all()
            for ply in range(max(self.by_ply, default=-1) + 1):
                for mine in (True, False):
                    if mine or self.cfg.include_opponent:
                        self._ply(ply, mine)
            self.finished = True
        except _Stop:
            self.finished = self.want < UNBOUNDED and self.found >= self.want and self.make

    def _units(self, ply: int, mine: bool) -> list[Unit]:
        out = []
        for pos in self.by_ply.get(ply, {}).values():
            played = {u: st for u, st in pos.moves.items() if (st.mine if mine else st.opp)}
            if not played or pos.key in self.legacy:
                continue
            pid = puzzle_id(pos.fen, ply)
            if pid in self.known or pid in self.merged:
                continue
            board = chess.Board(pos.fen)
            if board.legal_moves.count() < 2:
                continue
            u = Unit(pos, mine, board, played)
            for mv in board.legal_moves:
                b = board.copy(stack=False)
                b.push(mv)
                u.child[mv.uci()] = b.fen()
            out.append(u)
        return out

    def _ply(self, ply: int, mine: bool) -> None:
        units = self._units(ply, mine)
        if not units:
            return
        who = "your moves" if mine else "your opponents' moves"
        step = f"Ply {ply}, {who}: grading {len(units)} positions at depth {self.scan[0]} of {self.cfg.sf_depth}"
        self._step = step
        self.tracker.plies(ply, max(self.by_ply, default=0))
        self.tracker.found(self.found, 0 if self.want >= UNBOUNDED else self.want)
        self.tracker.budget(None if self.deadline == math.inf else max(0.0, (self.deadline - self.clock()) / 60))
        self.tracker.stage("analyse", self._msg(step), total=sum(len(u.child) for u in units), unit="moves",
                           step=f"depth {self.scan[0]} of {self.cfg.sf_depth}")
        for i, u in enumerate(units):
            self.tracker.position(f"ply {ply}: position {i + 1} of {len(units)}")
            self._search(u, 0)
        floor = self.cfg.min_loss_at(ply) / 2  # the low depths only rule out the clearly fine moves
        heap = [(-self.loss(u), i, u) for i, u in enumerate(units)]
        heapq.heapify(heap)
        n = len(units)
        while heap:
            self._tick()
            neg, _, u = heapq.heappop(heap)
            if -neg <= floor:
                break  # the heap is ordered: everything left is below too
            if u.level < len(self.scan) - 1:
                u.level += 1
                self._say(f"Ply {ply}, {who}: checking the biggest losses at depth {self.scan[u.level]} "
                          f"of {self.cfg.sf_depth}", f"depth {self.scan[u.level]} of {self.cfg.sf_depth}",
                          f"ply {ply}: a move losing {-neg:.0f} % · {len(heap) + 1} candidates")
                self._search(u, u.level)
                n += 1
                heapq.heappush(heap, (-self.loss(u), n, u))
            else:
                self._confirm(u, ply)
            if self.make and self.found >= self.want:
                raise _Stop()

    def _confirm(self, u: Unit, ply: int) -> None:
        """Full-strength look at the position's worst played moves; the first that is no good move makes the puzzle."""
        self._say(f"Ply {ply}: making a puzzle at full strength", f"depth {self.cfg.sf_depth} of {self.cfg.sf_depth}, every move",
                  f"ply {ply}: a move losing {self.loss(u):.0f} %")
        self.tracker.plan(1)
        game_opponent = ""
        worst = sorted(self._losses(u).items(), key=lambda kv: -kv[1])[:CONFIRM_TRIES]
        for move, loss in worst:
            if loss <= self.cfg.min_loss_at(ply) / 2:
                break
            st = u.played[move]
            game = (st.mine_game if u.mine else st.opp_game) or self.games[0]
            game_opponent = "" if u.mine else ((game.black if game.user_is_white else game.white) or "Your opponent")
            card = build_card(u.board.copy(stack=False), ply, move, self.an, self.cfg, game_opponent)
            self.tracker.advance()
            if card is None:
                continue
            self.found += 1
            self.tracker.found(self.found, 0 if self.want >= UNBOUNDED else self.want)
            if self.make:
                self._add(u, game, card)
            self._step = ""  # report the new count
            return

    # --- making notes
    def _scores_of(self, game: Game) -> list[Optional[Score]]:
        keys, b = [], chess.Board()
        keys.append(fen_key(b.fen()))
        for m in game.moves:
            b.push_uci(m)
            keys.append(fen_key(b.fen()))
        known = self.an.known(keys)
        return [(self.sc[k][0] if k in self.sc else (known[k][1] if k in known else None)) for k in keys]

    def _nags(self, game: Game, skip: set[int]) -> dict[int, int]:
        """The NAGs of a game's other moves, graded once per run (the analyser caches the searches)."""
        if game.key not in self._graded:
            label = f"Grading the moves of {game.white} – {game.black}"
            with self.tracker.sub("cards", label, total=len(game.moves), unit="positions", step="grading the moves"):
                self._graded[game.key] = game_nags(  # puzzle plies are graded in full already
                    game, self.an, self.cfg, skip, lambda i, n: self.tracker.advance() if i else None)
        return self._graded[game.key]

    def _stats_lines(self, p: Puzzle) -> list[str]:
        first = self.by_ply.get(p.ply, {}).get(fen_key(p.fen))
        after = self.by_ply.get(p.after_ply, {}).get(fen_key(p.after_fen)) if p.after_fen else None
        return stats.lines(first) + stats.lines(after, "After the mistake, ")

    def _notes_of(self, p: Puzzle) -> str:
        return notes_mod.puzzle_notes(p.head, self._stats_lines(p), [c for cs in p.links.values() for c in cs])

    def _add(self, u: Unit, game: Game, card: CardSpec) -> None:
        ply = card.ply
        similar_to = self._similar(u.board)
        if similar_to is not None:
            self._merge(similar_to, u, ply)
            return
        # The boards carry the game's moves up to the puzzle, for the repetition and fifty-move rules.
        hist = chess.Board()
        for m in game.moves[:ply]:
            hist.push_uci(m)
        after = hist.copy()
        played_san = hist.san(chess.Move.from_uci(card.played.move))
        after.push_uci(card.played.move)
        second = build_punish(after.copy(stack=False), ply + 1, played_san, self.an, self.cfg, mine=card.mine)
        seen = {fen_key(hist.fen()), fen_key(after.fen())}
        quick = self.scan[0]
        follow_puzzle(hist, card, self.an, self.cfg, solver=hist.turn, seen=seen, quick_depth=quick, tracker=self.tracker)
        if second is not None:
            follow_puzzle(after, second, self.an, self.cfg, solver=after.turn, seen=seen, quick_depth=quick,
                          include_played=True, one_move=bool(second.decided), tracker=self.tracker)
        skip = {ply}
        p = Puzzle(key=notes_mod.puzzle_key(card.fen_key, ply), fen=u.board.fen(), ply=ply,
                   moves={card.played.move: card.played.nag, **{c.move: c.nag for c in card.alternatives}},
                   played=card.played.move, after_fen=after.fen() if second else "", after_ply=ply + 1 if second else 0,
                   head=notes_mod.puzzle_head(game, card))
        p.notes = self._notes_of(p)
        spec = notes_mod.build_puzzle(game, self._scores_of(game), card, second, p.notes, self.cfg.inaccuracy,
                                      self._nags(game, skip))
        p.tags, p.pgn, p.cards, p.card_ids = spec.tags, spec.pgn, spec.cards, spec.card_ids
        self.store.put(p)
        self.known[p.id] = p
        self.made.add(p.key)
        self.notes.append(spec)
        self.on_note(spec)

    def _similar(self, board: chess.Board) -> Optional[Puzzle]:
        best: Optional[tuple[int, Puzzle]] = None
        for p in self.known.values():
            if not p.stored:
                continue
            other = chess.Board(p.fen)
            if similar.is_similar(other, board):
                n = len(similar.diff(other, board))
                if best is None or n < best[0]:
                    best = (n, p)
        return best[1] if best else None

    def _merge(self, p: Puzzle, u: Unit, ply: int) -> None:
        """Instead of a new note: `p`'s note gets a comment for each of its correct answers (and the mistake)."""
        old = chess.Board(p.fen)
        graded, _ = grade_position(u.board, self.an, self.cfg, None, ply)
        nag_here = {g.move: g.nag for g in graded}
        why = similar.describe(old, u.board)
        out = []
        for m in dict.fromkeys([*p.good(), p.played]):
            if m not in nag_here:  # not a legal move here: nothing to say
                continue
            out.append(similar.comment(old.san(chess.Move.from_uci(m)), p.moves.get(m) in ACCEPTED,
                                       nag_here[m] in ACCEPTED, why))
        bid = puzzle_id(u.pos.fen, ply)
        p.links[bid] = out
        self.merged.add(bid)
        self.store.put(p)
        self._refresh(p)

    # --- keeping existing notes up to date
    def _refresh(self, p: Puzzle) -> None:
        """Write `p`'s note again when its Notes field would now read differently (new games, new comments)."""
        if not self.make or (p.key not in self.existing and p.key not in self.made):
            return
        text = self._notes_of(p)
        if text == p.notes:
            return
        p.notes = text
        self.store.put(p)
        spec = NoteSpec(key=p.key, pgn=self.existing.get(p.key) or p.pgn, cards=p.cards, notes=text, tags=p.tags,
                        card_ids=p.card_ids, new_cards=0)
        self.notes.append(spec)
        self.on_note(spec)

    def refresh_all(self) -> None:
        for p in list(self.known.values()):
            if p.stored:
                self._refresh(p)

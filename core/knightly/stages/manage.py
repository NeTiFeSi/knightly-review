"""Stage 5, manage: the notes already in the collection.

Input: the collection's Knightly notes (note key → PGN) and their puzzle records. Settings (`cfg.manage`):

- `update_notes`: rebuild the notes made under older rules (`repair`, below): each is edited in place, keeping
  its cards, and gets whatever the old rules left out (a card is never removed); and refresh every note's Notes
  field when it would read differently (new games, comments from similar positions).
- `update_tags`: when a note is written again, its generated tags are replaced (off: they stay as they are).
- `suspend_outdated`: report the notes this version can no longer maintain (`outdated`), for the add-on to
  suspend: notes from versions before puzzle keys, and notes made under older rules whose game is gone (or all
  of those when `update_notes` is off).

Output: the rewritten notes (through the builder's `on_note`) and the list of outdated notes.
"""

from __future__ import annotations

import dataclasses
import io

import chess
import chess.pgn

from .. import notes as notes_mod
from ..cache import fen_key
from ..config import Config
from ..follow import follow_puzzle
from ..model import CardSpec, Choice, Game
from ..progress import Tracker
from ..puzzles import Puzzle, parse_key
from ..select import GOOD, build_card, grade_position
from .budget import Budget, Stop
from .build import Builder, _from_a_tree
from .report import Reporter


class Manager:
    def __init__(self, builder: Builder, cfg: Config, budget: Budget, report: Reporter, tracker: Tracker):
        self.b, self.cfg, self.budget, self.report, self.tracker = builder, cfg, budget, report, tracker
        self.an = builder.an
        self._by_url: dict[str, Game] | None = None
        self.old_rules = dict(builder.stale)  # the notes made under older rules, as the run starts
        if not cfg.manage.update_notes:
            builder.stale.clear()  # left as they are: no tree rebuilds them either

    def run(self) -> None:
        """Refresh the Notes fields, then rebuild the notes made under older rules (when the settings say so)."""
        if self.cfg.manage.update_notes:
            self.refresh_all()
            self.repair()

    def refresh_all(self) -> None:
        for p in list(self.b.ledger.known.values()):
            if p.stored:
                self.b.refresh(p)

    def _game_of(self, p: Puzzle) -> Game | None:
        """The game a note was made from, if it is among the run's games."""
        if self._by_url is None:
            self._by_url = {g.url: g for g in self.b.games if g.url}
        headers = chess.pgn.read_headers(io.StringIO(p.pgn))
        return self._by_url.get(headers.get("Site", "")) if headers else None

    def outdated(self) -> list[str]:
        """The keys of the notes this version can no longer maintain: notes from before puzzle keys, and notes
        still on older rules whose game is gone (all of them when notes are not updated)."""
        out = [k for k in self.b.existing if parse_key(k) is None]
        for key, p in self.old_rules.items():
            if key not in self.b.existing:
                continue
            if not self.cfg.manage.update_notes or (key in self.b.stale and self._game_of(p) is None):
                out.append(key)
        return out

    def repair(self) -> None:
        """Rebuild the notes made under older rules, the mistakes from our games first: growing their trees
        again also rebuilds the notes of the positions inside them and makes the ones that were missing. The
        notes of tree positions not reached that way are then rebuilt one by one. A note that cannot be
        rebuilt (its game is gone, or it is no puzzle under the current settings) stays."""
        if not self.b.make or not self.b.stale:
            return
        todo = sorted(self.b.stale.values(), key=lambda p: (_from_a_tree(p), p.ply))
        step = "Updating the notes made by an older version"
        self.report.stage(step, len(todo), "notes", "updating notes")
        for p in todo:
            if p.key in self.b.stale:  # not rebuilt along with an earlier one
                self.budget.tick()
                game = self._game_of(p)
                if game is not None:
                    (self._repair_tree_note if _from_a_tree(p) else self._repair_mistake)(p, game)
                if self.b.cards >= self.budget.want:
                    raise Stop()
            self.tracker.advance()

    def _repair_mistake(self, p: Puzzle, game: Game) -> None:
        board = chess.Board(p.fen)
        mine = (board.turn == chess.WHITE) == game.user_is_white
        opponent = "" if mine else ((game.black if game.user_is_white else game.white) or "Your opponent")
        card = build_card(board.copy(stack=False), p.ply, p.played, self.an, self.cfg, opponent)
        if card is not None:
            self.b.grow(p.fen, game, card)

    def _repair_tree_note(self, p: Puzzle, game: Game) -> None:
        """Grow the tree again from the position before the opponent's move, the note's first position."""
        pg = chess.pgn.read_game(io.StringIO(p.pgn))
        first = pg.next() if pg else None
        if pg is None or first is None:
            return
        board = pg.board()
        _, by_move = grade_position(board.copy(stack=False), self.an, self.cfg)
        line = by_move.get(first.move.uci())
        if line is None:
            return
        reply = Choice(move=first.move.uci(), nag=next(iter(first.nags), 0), score=line.score)
        start = CardSpec(fen_key=fen_key(board.fen()), ply=board.ply(), played=reply, alternatives=[reply], loss=0.0)
        after = chess.Board(p.fen)
        graded, _ = grade_position(after, self.an, self.cfg)
        start.mate = any(g.mate is not None and g.mate > 0 for g in graded)
        start.alternatives = [Choice(move=reply.move, nag=GOOD, score=reply.score)]  # a followed move
        decisions = follow_puzzle(
            board, start, self.an, self.cfg, solver=not board.turn, seen=set(), tracker=self.tracker, max_plies=3
        )
        for d in decisions:
            if notes_mod.puzzle_key(fen_key(d.fen), d.ply) == p.key:
                d.reply = dataclasses.replace(d.reply, nag=reply.nag)
                self.b.sub_note(d, game)

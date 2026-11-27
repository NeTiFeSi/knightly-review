"""The one way puzzles are made: ply by ply, the biggest mistakes confirmed first.

For ply 0, 1, 2… (half-moves already played), the user's positions first and then, with
`cfg.select.include_opponent`, the opponents':

1. Every position at that ply gets a cheap scan (`Analyser.scan`, at `cfg.annotate.scan_depth`): the best move with one
   line, and each move the games played from the position searched on its own. Each position gets the loss
   of the worst move a player actually played there: how far its win rate falls below the best move's
   (engine WDL).
2. The positions where that loss is more than half of `min_loss` (the scan is shallow: it only rules out
   the clearly fine moves) are taken biggest first. Each gets the full search, to `cfg.annotate.sf_depth` with one line
   per legal move, and the full setup (`select.build_card`): if the played move is more than `min_loss` worse
   than the best and not a good move, it is a puzzle: one note with two puzzles, the position before the
   mistake and the position after it (the punishment).
3. Then the next ply.

A puzzle very similar to one already made is not a note of its own: the old puzzle's note gets a
comment for each correct answer instead (`similar.py`). How often each move was played in a
position (`stats.py`) goes in the note's Notes field and is refreshed when the games change.

Every search is cached by position and depth, so a run carries on where the last one stopped.

Notes built under older rules (`puzzles.RULES`) are rebuilt first (`Miner.repair`): each is edited in place,
keeping its cards, and gets whatever the old rules left out. A card is never removed.
"""

from __future__ import annotations

import dataclasses
import io
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import chess
import chess.pgn

from . import notes as notes_mod
from . import similar, stats, statsview
from .cache import fen_key
from .config import Config
from .follow import Decision, follow_puzzle, trimmed
from .model import CardSpec, Choice, Game, Line, NoteSpec, Score
from .progress import Tracker
from .puzzles import RULES, Puzzle, Store, puzzle_id
from .select import ACCEPTED, GOOD, build_card, build_punish, game_nags, grade_position

UNBOUNDED = 10**6  # `want` at or above this: no cap, keep confirming (pre-calculating)
CONFIRM_TRIES = 3  # worst moves of a position tried at full strength before giving up on it

Progress = Callable[[str, int, int, str], None]


class _Stop(Exception):
    """The time is up or enough puzzles are made."""


def _from_a_tree(p: Puzzle) -> bool:
    """A note of a position inside a tree starts from that position, not from the game's first move."""
    return '[FEN "' in p.pgn


@dataclass(eq=False)
class Unit:
    """One position at one ply, as seen from one side: the moves that side played from it."""

    pos: stats.Pos
    mine: bool
    board: chess.Board
    played: dict[str, stats.MoveStat]
    scan: tuple[Line, dict[str, Line]] | None = None  # the scan: the best line, and the line of each played move


class Miner:
    def __init__(
        self,
        games: Iterable[Game],
        analyser,
        cfg: Config,
        store: Store,
        *,
        want: int,
        known: Iterable[Puzzle] = (),
        claimed: Iterable[str] = (),
        existing: dict[str, str] | None = None,
        make: bool = True,
        tracker: Tracker | None = None,
        on_note: Callable[[NoteSpec], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`games` newest first. `known`: the puzzles already in the collection; `claimed`: fen keys of
        the positions of every card already in the collection (never made twice, whatever the ply); `existing`: note key → PGN of the collection's notes.
        `make=False` (pre-calculating): analyse and warm the cache, make and change no notes."""
        self.games = list(games)
        self.by_key = {g.key: g for g in self.games}
        self.an, self.cfg, self.store, self.want, self.make = analyser, cfg, store, want, make
        self.claimed = set(claimed)
        self.existing = dict(existing or {})
        self.tracker = tracker or Tracker()
        self.on_note = on_note or (lambda spec: None)
        self.clock = clock
        self.deadline = clock() + cfg.build.analysis_minutes * 60 if cfg.build.analysis_minutes > 0 else math.inf
        self.known: dict[str, Puzzle] = {p.id: p for p in known}
        self.merged = {bid for p in self.known.values() for bid in p.links}
        # Notes in the collection built under older rules, by note key, until they are rebuilt.
        self.stale = {p.key: p for p in self.known.values() if p.stored and p.rules < RULES and p.key in self.existing}
        self.by_ply = stats.collect(self.games)
        self.by_fen = stats.collect_positions(self.games)  # for the Notes field: matched whatever the ply
        self._graded: dict[str, dict[int, int]] = {}
        self.notes: list[NoteSpec] = []
        self.made: set[str] = set()  # keys of the notes made in this run
        self.found = 0  # mistakes confirmed (made, or only found when `make` is off)
        self.cards = 0  # cards made in this run (what `want` limits)
        self.analysed = 0  # positions searched by the engine in this run
        self.finished = False  # nothing left to continue: the run ended by itself, not out of time
        self.unconfirmed = False  # a repaired note was written that only the next run can see arrived
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
        done = self.cards if self.make else self.found
        what = "cards made" if self.make else "puzzles found"
        if self.want >= UNBOUNDED:
            return f"{step} ({done} {what}{budget})"
        return f"{step} ({done} of {self.want} {what}{budget})"

    def _say(self, step: str, detail: str = "", position: str | None = None) -> None:
        """Change the message of the current stage (a forced update when it differs)."""
        self.tracker.budget(None if self.deadline == math.inf else max(0.0, (self.deadline - self.clock()) / 60))
        if position is not None:
            self.tracker.position(position)
        self.tracker.message(self._msg(step), step=detail, force=step != self._step)
        self._step = step

    def _losses(self, u: Unit) -> dict[str, float]:
        """Loss of each played move against the best move, from the unit's scan."""
        if u.scan is None:
            return {}
        best, lines = u.scan
        white = u.board.turn == chess.WHITE
        top = best.score.win_pct(white)
        return {m: max(0.0, top - line.score.win_pct(white)) for m, line in lines.items()}

    def loss(self, u: Unit) -> float:
        return max(self._losses(u).values(), default=0.0)

    # --- the loop
    def run(self) -> None:
        try:
            self.refresh_all()
            self.repair()
            for ply in range(max(self.by_ply, default=-1) + 1):
                for mine in (True, False):
                    if mine or self.cfg.select.include_opponent:
                        self._ply(ply, mine)
            self.finished = True
        except _Stop:
            self.finished = self.want < UNBOUNDED and self.cards >= self.want and self.make

    def _units(self, ply: int, mine: bool) -> list[Unit]:
        out = []
        for pos in self.by_ply.get(ply, {}).values():
            played = {u: st for u, st in pos.moves.items() if (st.mine if mine else st.opp)}
            if not played or pos.key in self.claimed:
                continue
            pid = puzzle_id(pos.fen, ply)
            if pid in self.known or pid in self.merged:
                continue
            board = chess.Board(pos.fen)
            if board.legal_moves.count() < 2:
                continue
            out.append(Unit(pos, mine, board, played))
        return out

    def _ply(self, ply: int, mine: bool) -> None:
        units = self._units(ply, mine)
        if not units:
            return
        who = "your moves" if mine else "your opponents' moves"
        depth = self.cfg.annotate.sf_depth
        scan_depth = self.cfg.annotate.scan_depth_used()
        step = f"Ply {ply}, {who}: scanning {len(units)} positions at depth {scan_depth} of {depth}"
        self._step = step
        self.tracker.plies(ply, max(self.by_ply, default=0))
        self.tracker.found(self.cards if self.make else self.found, 0 if self.want >= UNBOUNDED else self.want)
        self.tracker.budget(None if self.deadline == math.inf else max(0.0, (self.deadline - self.clock()) / 60))
        self.tracker.stage("analyse", self._msg(step), total=len(units), unit="positions", step=f"depth {scan_depth}")
        for i, u in enumerate(units):
            self._tick()
            self.tracker.position(f"ply {ply}: position {i + 1} of {len(units)}")
            u.scan = self.an.scan(u.board, list(u.played))
            self.analysed += 1
            self.tracker.advance()
        floor = self.cfg.select.min_loss_at(ply) / 2  # the scan is shallow: it only rules out the clearly fine moves
        for u in sorted((u for u in units if self.loss(u) > floor), key=lambda u: -self.loss(u)):
            self._tick()
            self._confirm(u, ply)
            if self.make and self.cards >= self.want:
                raise _Stop()

    def _confirm(self, u: Unit, ply: int) -> None:
        """Full-strength look at the position's worst played moves; the first that is no good move makes the
        puzzle."""
        self._say(
            f"Ply {ply}: making a puzzle at full strength",
            f"depth {self.cfg.annotate.sf_depth}, every move",
            f"ply {ply}: a move losing {self.loss(u):.0f} %",
        )
        self.tracker.plan(1)
        game_opponent = ""
        worst = sorted(self._losses(u).items(), key=lambda kv: -kv[1])[:CONFIRM_TRIES]
        for move, loss in worst:
            if loss <= self.cfg.select.min_loss_at(ply) / 2:
                break
            st = u.played[move]
            game = (st.mine_game if u.mine else st.opp_game) or self.games[0]
            game_opponent = "" if u.mine else ((game.black if game.user_is_white else game.white) or "Your opponent")
            card = build_card(u.board.copy(stack=False), ply, move, self.an, self.cfg, game_opponent)
            self.tracker.advance()
            if card is None:
                continue
            self.found += 1
            self.tracker.found(self.cards if self.make else self.found, 0 if self.want >= UNBOUNDED else self.want)
            if self.make:
                self._add(u, game, card)
            self._step = ""  # report the new count
            return

    # --- making notes
    def _scores_of(self, game: Game) -> list[Score | None]:
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

    def _nags(self, game: Game, skip: set[int]) -> dict[int, int]:
        """The NAGs of a game's other moves, graded once per run (the analyser caches the searches)."""
        if game.key not in self._graded:
            label = f"Grading the moves of {game.white} – {game.black}"
            with self.tracker.sub("cards", label, total=len(game.moves), unit="positions", step="grading the moves"):
                self._graded[game.key] = game_nags(  # puzzle plies are graded in full already
                    game, self.an, self.cfg, skip, lambda i, n: self.tracker.advance() if i else None
                )
        return self._graded[game.key]

    def _stats_sections(self, p: Puzzle) -> list[str]:
        """The explorer tables of the note's two positions (each only when two games reached it)."""
        out = [statsview.section(1, self.by_fen.get(fen_key(p.fen)), nags=p.moves, mistake=p.played)]
        if p.after_fen:
            out.append(
                statsview.section(2, self.by_fen.get(fen_key(p.after_fen)), "After the mistake: ", nags=p.after_moves)
            )
        return out

    def _notes_of(self, p: Puzzle) -> str:
        return notes_mod.puzzle_notes(p.head, self._stats_sections(p), [c for cs in p.links.values() for c in cs])

    def _add(self, u: Unit, game: Game, card: CardSpec) -> None:
        """The notes of a mistake: one for every decision position of its two trees, deepest first, then the
        mistake's own. A position that is a card already is never made again. Stops between notes at the
        card limit (the mistake is found again next time and only the missing notes are made)."""
        similar_to = self._similar(u.board)
        if similar_to is not None:
            self._merge(similar_to, u, card.ply)
            return
        self._grow(u.board.fen(), game, card)

    def _grow(self, fen: str, game: Game, card: CardSpec) -> None:
        """`_add` without the similarity check, for the mistake at `fen`."""
        ply = card.ply
        # The boards carry the game's moves up to the puzzle, for the repetition and fifty-move rules.
        hist = chess.Board()
        for m in game.moves[:ply]:
            hist.push_uci(m)
        after = hist.copy()
        played_san = hist.san(chess.Move.from_uci(card.played.move))
        after.push_uci(card.played.move)
        second = build_punish(after.copy(stack=False), ply + 1, played_san, self.an, self.cfg, mine=card.mine)
        seen = {fen_key(hist.fen()), fen_key(after.fen())}
        decisions = follow_puzzle(hist, card, self.an, self.cfg, solver=hist.turn, seen=seen, tracker=self.tracker)
        if second is not None:
            decisions += follow_puzzle(
                after,
                second,
                self.an,
                self.cfg,
                solver=after.turn,
                seen=seen,
                include_played=True,
                tracker=self.tracker,
            )
        for d in sorted(decisions, key=lambda d: -d.depth):  # the leaves first, then towards the root
            self._sub_note(d, game)
            if self.cards >= self.want:
                raise _Stop()
        self._root_note(fen, game, card, second, after)

    def _root_note(self, fen: str, game: Game, card: CardSpec, second: CardSpec | None, after: chess.Board) -> None:
        ply = card.ply
        key = notes_mod.puzzle_key(card.fen_key, ply)
        self._release(key)
        if second is not None and fen_key(after.fen()) in self.claimed:
            second = None  # that position is a card already
        # A card is a move and the reply to it: what the trees hold below is the notes of its own.
        card.alternatives = [trimmed(c, 1) for c in card.alternatives]
        if second is not None:
            second.played, second.alternatives = trimmed(second.played, 1), [trimmed(c, 1) for c in second.alternatives]
        p = Puzzle(
            key=key,
            fen=fen,
            ply=ply,
            moves={card.played.move: card.played.nag, **{c.move: c.nag for c in card.alternatives}},
            played=card.played.move,
            after_fen=after.fen() if second else "",
            after_ply=ply + 1 if second else 0,
            head=notes_mod.puzzle_head(game, card),
            after_moves=self._moves_of(second),
        )
        self._write_notes_field(p)
        spec = notes_mod.build_puzzle(
            game, self._scores_of(game), card, second, p.notes, self.cfg.annotate.inaccuracy, self._nags(game, {ply})
        )
        self._emit(p, spec)

    @staticmethod
    def _moves_of(card: CardSpec | None) -> dict[str, int]:
        return {} if card is None else {card.played.move: card.played.nag, **{c.move: c.nag for c in card.alternatives}}

    def _write_notes_field(self, p: Puzzle) -> None:
        if p.key in self.stale:  # the comments from similar positions stay
            p.links = dict(self.stale[p.key].links)
        p.notes = self._notes_of(p)

    def _release(self, key: str) -> None:
        """The positions of a note being rebuilt are free for it again."""
        if key in self.stale:
            self.claimed.difference_update(self.stale[key].card_ids)

    def _emit(self, p: Puzzle, spec: NoteSpec) -> None:
        p.tags, p.pgn, p.cards, p.card_ids = spec.tags, spec.pgn, spec.cards, spec.card_ids
        p.rules = RULES
        old = self.stale.pop(p.key, None)
        if old is not None:
            have = notes_mod.card_ids(self.existing[p.key])  # the cards the note in the collection has
            self.claimed.update(have)
            if set(have) - set(spec.card_ids):  # it would lose a card: the note stays as it is, to be tried again
                return
            spec.new_cards = len(set(spec.card_ids) - set(have))
            if spec.pgn != self.existing[p.key]:
                # Written below, but only the next run sees whether it reached the collection: until then
                # the record stays on the old rules.
                p.rules = old.rules
                self.unconfirmed = True
        self.store.put(p)
        self.known[p.id] = p
        self.claimed.update(spec.card_ids)
        if old is not None:
            if spec.pgn == self.existing[p.key]:
                return  # the note is as the current rules make it
            self.existing[p.key] = spec.pgn
        self.made.add(p.key)
        self.cards += spec.new_cards
        self.notes.append(spec)
        self.on_note(spec)
        self.tracker.found(self.cards, 0 if self.want >= UNBOUNDED else self.want)

    def _sub_note(self, d: Decision, game: Game) -> None:
        """The note of a decision position X: find the best move there (with the opponent's reply), and punish the
        best move that is not good (the position after it). Not made when X is a card already; the second card
        is left out when its position is a card already or the game is decided there."""
        key = fen_key(d.fen)
        pkey = notes_mod.puzzle_key(key, d.ply)
        old = self.stale.get(pkey)
        rebuild = old is not None and _from_a_tree(old)
        mistake = next((c for c in d.choices if c.move not in d.good), None)  # the best move that is not good
        if (key in self.claimed and not rebuild) or mistake is None:
            return
        seen = self.by_fen.get(key)
        if seen is not None:  # a move that is not good and that our games played there is the better mistake
            played = [c for c in d.choices if c.move not in d.good and c.move in seen.moves]
            if played:
                mistake = max(played, key=lambda c: seen.moves[c.move].games)
        if rebuild:  # the note keeps asking about its own mistake, whatever the games played since
            mistake = next((c for c in d.choices if c.move == old.played and c.move not in d.good), mistake)
        board = chess.Board(d.fen)
        user = chess.WHITE if game.user_is_white else chess.BLACK
        mine = board.turn == user
        best = d.choices[0]
        white = board.turn == chess.WHITE
        loss = max(0.0, best.score.win_pct(white) - mistake.score.win_pct(white))
        best_san = board.san(chess.Move.from_uci(best.move))
        mistake_san = board.san(chess.Move.from_uci(mistake.move))
        comment = (
            f"{mistake_san} loses {loss:.0f}% win rate. {best_san} was best."
            if mine
            else f"{mistake_san} loses {loss:.0f}% win rate for your opponent here. {best_san} was best."
        )
        others = [trimmed(c, 1) for c in d.choices if c is not mistake]
        first = CardSpec(
            fen_key=key,
            ply=d.ply,
            played=trimmed(mistake, 0),
            alternatives=others,
            loss=loss,
            comment=comment,
            mine=mine,
        )
        self._release(pkey)
        after = board.copy(stack=False)
        after.push_uci(mistake.move)
        second = None
        if fen_key(after.fen()) not in self.claimed:
            second = build_punish(after.copy(stack=False), d.ply + 1, mistake_san, self.an, self.cfg, mine=mine)
        if second is not None:
            follow_puzzle(
                after,
                second,
                self.an,
                self.cfg,
                solver=after.turn,
                seen=set(),
                include_played=True,
                tracker=self.tracker,
                max_plies=2,
            )
            second.played = trimmed(second.played, 1)
            second.alternatives = [trimmed(c, 1) for c in second.alternatives]
        p = Puzzle(
            key=pkey,
            fen=d.fen,
            ply=d.ply,
            moves={mistake.move: mistake.nag, **{c.move: c.nag for c in others}},
            played=mistake.move,
            after_fen=after.fen() if second else "",
            after_ply=d.ply + 1 if second else 0,
            head=notes_mod.puzzle_head(game, first),
            after_moves=self._moves_of(second),
        )
        self._write_notes_field(p)
        spec = notes_mod.build_puzzle(
            game, [], first, second, p.notes, self.cfg.annotate.inaccuracy, lead=(d.before_fen, trimmed(d.reply, 0))
        )
        self._emit(p, spec)

    def _similar(self, board: chess.Board) -> Puzzle | None:
        best: tuple[int, Puzzle] | None = None
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
        graded, _ = grade_position(u.board, self.an, self.cfg)
        nag_here = {g.move: g.nag for g in graded}
        why = similar.describe(old, u.board)
        out = []
        for m in dict.fromkeys([*p.good(), p.played]):
            if m not in nag_here:  # not a legal move here: nothing to say
                continue
            out.append(
                similar.comment(
                    old.san(chess.Move.from_uci(m)), p.moves.get(m) in ACCEPTED, nag_here[m] in ACCEPTED, why
                )
            )
        bid = puzzle_id(u.pos.fen, ply)
        p.links[bid] = out
        self.merged.add(bid)
        self.store.put(p)
        self._refresh(p)

    # --- keeping existing notes up to date
    def repair(self) -> None:
        """Rebuild the notes made under older rules, the mistakes from our games first: growing their trees
        again also rebuilds the notes of the positions inside them and makes the ones that were missing. The
        notes of tree positions not reached that way are then rebuilt one by one. A note that cannot be
        rebuilt (its game is gone, or it is no puzzle under the current settings) stays."""
        if not self.make or not self.stale:
            return
        by_url = {g.url: g for g in self.games if g.url}
        todo = sorted(self.stale.values(), key=lambda p: (_from_a_tree(p), p.ply))
        step = "Updating the notes made by an older version"
        self._step = step
        self.tracker.stage("analyse", self._msg(step), total=len(todo), unit="notes", step="updating notes")
        for p in todo:
            if p.key in self.stale:  # not rebuilt along with an earlier one
                self._tick()
                headers = chess.pgn.read_headers(io.StringIO(p.pgn))
                game = by_url.get(headers.get("Site", "")) if headers else None
                if game is not None:
                    (self._repair_tree_note if _from_a_tree(p) else self._repair_mistake)(p, game)
                if self.cards >= self.want:
                    raise _Stop()
            self.tracker.advance()

    def _repair_mistake(self, p: Puzzle, game: Game) -> None:
        board = chess.Board(p.fen)
        mine = (board.turn == chess.WHITE) == game.user_is_white
        opponent = "" if mine else ((game.black if game.user_is_white else game.white) or "Your opponent")
        card = build_card(board.copy(stack=False), p.ply, p.played, self.an, self.cfg, opponent)
        if card is not None:
            self._grow(p.fen, game, card)

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
                self._sub_note(d, game)

    def _refresh(self, p: Puzzle) -> None:
        """Write `p`'s note again when its Notes field would now read differently (new games, new comments)."""
        if not self.make or (p.key not in self.existing and p.key not in self.made):
            return
        text = self._notes_of(p)
        if text == p.notes:
            return
        p.notes = text
        self.store.put(p)
        pgn = self.existing.get(p.key) or p.pgn
        ids = notes_mod.card_ids(pgn) or p.card_ids  # of the PGN written: the record may be ahead of the note
        spec = NoteSpec(
            key=p.key,
            pgn=pgn,
            cards=notes_mod.clozes(ids.values()),
            notes=text,
            tags=p.tags,
            card_ids=ids,
            new_cards=0,
        )
        self.notes.append(spec)
        self.on_note(spec)

    def refresh_all(self) -> None:
        for p in list(self.known.values()):
            if p.stored:
                self._refresh(p)

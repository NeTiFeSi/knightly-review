"""Stage 4, build: puzzles and their notes, from the candidates.

Input: the candidates (stage 3), most promising first. Each gets the full search, to `cfg.annotate.sf_depth` with
one line per legal move, and the full setup (`select.build_card`): if a played move is more than `min_loss` worse
than the best and not a good move, it is a puzzle: one note with two puzzles, the position before the mistake and
the position after it (the punishment), plus one note for each later position of its solution tree where the
choice matters (deepest first). Output: the notes (`NoteSpec`, handed to `on_note` as soon as each is built) and
the puzzle records (`puzzles.Store`).

A puzzle very similar to one already made is not a note of its own: the old puzzle's note gets a comment for each
correct answer instead (`similar.py`). How often each move was played in a position (`stats.py`) goes in the
note's Notes field and is refreshed when the games change.

Notes built under older rules (`puzzles.RULES`) are rebuilt (`repair`): each is edited in place, keeping its
cards, and gets whatever the old rules left out. A card is never removed. (Repairing and refreshing the notes
already in the collection belong to the manage stage; they move there next.)
"""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Callable, Iterable

import chess
import chess.pgn

from .. import notes as notes_mod
from .. import similar, stats, statsview
from ..cache import fen_key
from ..config import Config
from ..follow import Decision, follow_puzzle, trimmed
from ..model import CardSpec, Choice, Game, NoteSpec
from ..progress import Tracker
from ..puzzles import RULES, Puzzle, Store, puzzle_id
from ..select import ACCEPTED, GOOD, build_card, build_punish, grade_position
from .annotate import Annotator, Unit
from .budget import Budget, Stop
from .candidates import Ledger
from .report import Reporter


def _from_a_tree(p: Puzzle) -> bool:
    """A note of a position inside a tree starts from that position, not from the game's first move."""
    return '[FEN "' in p.pgn


class Builder:
    def __init__(
        self,
        games: list[Game],
        analyser,
        cfg: Config,
        store: Store,
        ledger: Ledger,
        annotator: Annotator,
        budget: Budget,
        report: Reporter,
        tracker: Tracker,
        *,
        existing: dict[str, str] | None = None,
        make: bool = True,
        on_note: Callable[[NoteSpec], None] | None = None,
    ):
        """`games` newest first. `existing`: note key → PGN of the collection's notes. `make=False`
        (pre-calculating): confirm candidates and warm the cache, make and change no notes."""
        self.games = games
        self.an, self.cfg, self.store, self.ledger = analyser, cfg, store, ledger
        self.annotator, self.budget, self.report, self.tracker = annotator, budget, report, tracker
        self.existing = dict(existing or {})
        self.make = make
        self.on_note = on_note or (lambda spec: None)
        # Notes in the collection built under older rules, by note key, until they are rebuilt.
        self.stale = {
            p.key: p for p in ledger.known.values() if p.stored and p.rules < RULES and p.key in self.existing
        }
        self.by_fen = stats.collect_positions(games)  # for the Notes field: matched whatever the ply
        self.notes: list[NoteSpec] = []
        self.made: set[str] = set()  # keys of the notes made in this run
        self.found = 0  # mistakes confirmed (made, or only found when `make` is off)
        self.cards = 0  # cards made in this run (what `budget.want` limits)
        self.unconfirmed = False  # a repaired note was written that only the next run can see arrived

    def confirm(self, u: Unit, ply: int, tries: Iterable[tuple[str, float]]) -> None:
        """Full-strength look at the candidate's worst played moves; the first that is no good move makes the
        puzzle."""
        self.report.say(
            f"Ply {ply}: making a puzzle at full strength",
            f"depth {self.cfg.annotate.sf_depth}, every move",
            f"ply {ply}: a move losing {u.loss():.0f} %",
        )
        self.tracker.plan(1)
        for move, _loss in tries:
            st = u.played[move]
            game = (st.mine_game if u.mine else st.opp_game) or self.games[0]
            game_opponent = "" if u.mine else ((game.black if game.user_is_white else game.white) or "Your opponent")
            card = build_card(u.board.copy(stack=False), ply, move, self.an, self.cfg, game_opponent)
            self.tracker.advance()
            if card is None:
                continue
            self.found += 1
            self.report.found()
            if self.make:
                self._add(u, game, card)
            self.report.step = ""  # report the new count
            return

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
            if self.cards >= self.budget.want:
                raise Stop()
        self._root_note(fen, game, card, second, after)

    def _root_note(self, fen: str, game: Game, card: CardSpec, second: CardSpec | None, after: chess.Board) -> None:
        ply = card.ply
        key = notes_mod.puzzle_key(card.fen_key, ply)
        self._release(key)
        if second is not None and fen_key(after.fen()) in self.ledger.claimed:
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
            game,
            self.annotator.scores_of(game),
            card,
            second,
            p.notes,
            self.cfg.annotate.inaccuracy,
            self.annotator.nags(game, {ply}),
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
            self.ledger.claimed.difference_update(self.stale[key].card_ids)

    def _emit(self, p: Puzzle, spec: NoteSpec) -> None:
        p.tags, p.pgn, p.cards, p.card_ids = spec.tags, spec.pgn, spec.cards, spec.card_ids
        p.rules = RULES
        old = self.stale.pop(p.key, None)
        if old is not None:
            have = notes_mod.card_ids(self.existing[p.key])  # the cards the note in the collection has
            self.ledger.claimed.update(have)
            if set(have) - set(spec.card_ids):  # it would lose a card: the note stays as it is, to be tried again
                return
            spec.new_cards = len(set(spec.card_ids) - set(have))
            if spec.pgn != self.existing[p.key]:
                # Written below, but only the next run sees whether it reached the collection: until then
                # the record stays on the old rules.
                p.rules = old.rules
                self.unconfirmed = True
        self.store.put(p)
        self.ledger.known[p.id] = p
        self.ledger.claimed.update(spec.card_ids)
        if old is not None:
            if spec.pgn == self.existing[p.key]:
                return  # the note is as the current rules make it
            self.existing[p.key] = spec.pgn
        self.made.add(p.key)
        self.cards += spec.new_cards
        self.notes.append(spec)
        self.on_note(spec)
        self.tracker.found(self.cards, self.budget.want if self.budget.capped else 0)

    def _sub_note(self, d: Decision, game: Game) -> None:
        """The note of a decision position X: find the best move there (with the opponent's reply), and punish the
        best move that is not good (the position after it). Not made when X is a card already; the second card
        is left out when its position is a card already or the game is decided there."""
        key = fen_key(d.fen)
        pkey = notes_mod.puzzle_key(key, d.ply)
        old = self.stale.get(pkey)
        rebuild = old is not None and _from_a_tree(old)
        mistake = next((c for c in d.choices if c.move not in d.good), None)  # the best move that is not good
        if (key in self.ledger.claimed and not rebuild) or mistake is None:
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
        if fen_key(after.fen()) not in self.ledger.claimed:
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
        for p in self.ledger.known.values():
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
        self.ledger.merged.add(bid)
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
        self.report.stage(step, len(todo), "notes", "updating notes")
        for p in todo:
            if p.key in self.stale:  # not rebuilt along with an earlier one
                self.budget.tick()
                headers = chess.pgn.read_headers(io.StringIO(p.pgn))
                game = by_url.get(headers.get("Site", "")) if headers else None
                if game is not None:
                    (self._repair_tree_note if _from_a_tree(p) else self._repair_mistake)(p, game)
                if self.cards >= self.budget.want:
                    raise Stop()
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
        for p in list(self.ledger.known.values()):
            if p.stored:
                self._refresh(p)

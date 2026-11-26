"""Write a puzzle (the mistake and its punishment) as a note in the Knightly Review PGN contract (see card/README.md)."""

from __future__ import annotations

import html
import io
import re
import time
from typing import Optional

import chess
import chess.pgn

from . import openings, phases
from .cache import fen_key
from .engine.analyse import MATED
from .model import CardSpec, Choice, Game, NoteSpec, Score
from .config import Config
from .puzzles import parse_key, puzzle_key
from .select import BLUNDER, DUBIOUS, MISTAKE, quick_nag

KR_TAG = re.compile(r"\[%kr\s+(\d+)\s*\]")
NOTE_TAG = "knightly"
PUZZLE_TAG = f"{NOTE_TAG}::puzzle::"
_PUZZLE_TAG_RE = re.compile(re.escape(PUZZLE_TAG) + r"(\d+)-([0-9a-f]+)$", re.I)
SEVERITY = {BLUNDER: "blunder", MISTAKE: "mistake", DUBIOUS: "inaccuracy"}
RESULT = {1.0: "win", 0.5: "draw", 0.0: "loss"}


def note_key_tag(key: str) -> str:
    """Anki tag that finds a note again: key puzzle:12:ab12cd34ef56 → knightly::puzzle::012-ab12cd34ef56."""
    parsed = parse_key(key)
    if parsed is None:
        raise ValueError(f"not a note key: {key}")
    return f"{PUZZLE_TAG}{parsed[0]:03d}-{parsed[1]}"


def key_from_tags(tags: list[str]) -> Optional[str]:
    """The note key that `note_key_tag` made, from a note's tags (None for a note that is not a puzzle note)."""
    for t in tags:
        m = _PUZZLE_TAG_RE.match(t)
        if m:
            return f"puzzle:{int(m[1])}:{m[2].lower()}"
    return None


def field_to_pgn(field: str) -> str:
    return html.unescape(re.sub(r"<br\s*/?>", "\n", field, flags=re.I))


def pgn_to_field(pgn: str) -> str:
    return html.escape(pgn, quote=False).replace("\n", "<br>")


def card_ids(pgn: str) -> dict[str, int]:
    """fen_key of each tagged puzzle position → its cloze number, for every game in the PGN."""
    out: dict[str, int] = {}
    f = io.StringIO(pgn)
    while True:
        g = chess.pgn.read_game(f)
        if g is None:
            break
        for node in g.mainline():
            m = KR_TAG.search(node.comment or "")
            if m:
                parent = node.parent
                assert parent is not None
                out[fen_key(parent.board().fen())] = int(m.group(1))
    return out


def _eval(s: Optional[Score]) -> str:
    if s is None or (s.mate is None and abs(s.cp or 0) >= MATED):
        return ""
    return f"[%eval {s.pgn()}]"


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _write_choice(parent: chess.pgn.GameNode, ch: Choice) -> chess.pgn.GameNode:
    """`ch` as a move after `parent`, with the moves that follow it (the first one is the main line)."""
    v = parent.add_variation(chess.Move.from_uci(ch.move))
    v.nags = {ch.nag} - {0}
    v.comment = _join(_eval(ch.score), f"[%kr {ch.tag}]" if ch.tag else "", ch.comment)
    for child in ch.children:
        _write_choice(v, child)
    return v


def write_card(parent: chess.pgn.GameNode, node: chess.pgn.GameNode, card: CardSpec, number: int,
               after: Optional[Score] = None) -> None:
    """Tag `node` (the played move, after `parent`) as cloze `number`, add the card's alternatives as variations
    and what follows the played move (the punishment) as its continuation."""
    node.nags = {card.played.nag} - {0}
    node.comment = _join(_eval(card.played.score or after), f"[%kr {number}]", card.comment, card.played.comment)
    for alt in card.alternatives:
        _write_choice(parent, alt)
    for child in card.played.children:
        _write_choice(node, child)


def _puzzle_pgn(game: Game, scores: list[Optional[Score]], first: CardSpec, inaccuracy: float = Config.inaccuracy,
                nags: Optional[dict[int, int]] = None, lead: Optional[tuple[str, Choice]] = None) -> str:
    """The game up to the mistake (every earlier move with its NAG: `nags`, ply → NAG, see select.game_nags,
    else ?!, ?, ?? from the loss shown by `scores`), then the mistake tagged `[%kr 1]` with the solution trees.
    `lead` = (FEN, move): instead of the game's moves, the note starts from that position and plays that move (the
    opponent's reply that led to `first`'s position, for a note made from a position inside a tree)."""
    pg = chess.pgn.Game()
    h = pg.headers
    h["Event"] = game.event or "?"
    h["Site"] = game.url or "?"
    h["Date"] = _date(game, ".")
    h["White"] = game.white or "?"
    h["Black"] = game.black or "?"
    h["Result"] = game.result
    if game.white_elo:
        h["WhiteElo"] = game.white_elo
    if game.black_elo:
        h["BlackElo"] = game.black_elo
    del h["Round"]

    if lead is not None:
        h["FEN"], h["SetUp"] = lead[0], "1"
        r = pg.add_variation(chess.Move.from_uci(lead[1].move))
        r.nags = {lead[1].nag} - {0}
        r.comment = _join(_eval(lead[1].score), lead[1].comment)
        parent = r
        node = parent.add_variation(chess.Move.from_uci(first.played.move))
        write_card(parent, node, first, 1, first.played.score)
        return pg.accept(chess.pgn.StringExporter(headers=True, variations=True, comments=True))
    node: chess.pgn.GameNode = pg
    for ply, u in enumerate(game.moves[: first.ply + 1]):
        parent = node
        node = parent.add_variation(chess.Move.from_uci(u))
        after = scores[ply + 1] if ply + 1 < len(scores) else None
        if ply == first.ply:
            write_card(parent, node, first, 1, after)
            break
        node.comment = _eval(after)
        before = scores[ply] if ply < len(scores) else None
        if ply in (nags or {}):
            node.nags = {(nags or {})[ply]}
        elif before is not None and after is not None and before.wdl and after.wdl:
            white = ply % 2 == 0
            node.nags = {quick_nag(before.win_pct(white) - after.win_pct(white), inaccuracy)} - {0}
    return pg.accept(chess.pgn.StringExporter(headers=True, variations=True, comments=True))


def _date(game: Game, sep: str = "-") -> str:
    if not game.end_ts:
        return sep.join(["????", "??", "??"])
    return time.strftime(f"%Y{sep}%m{sep}%d", time.gmtime(game.end_ts))


def _game_link(game: Game) -> str:
    if not game.url:
        return ""
    return f'<a href="{html.escape(game.url)}">{html.escape(game.white)} – {html.escape(game.black)}</a>, {_date(game)}'


def card_phase(game: Game, card: CardSpec) -> str:
    return phases.phase_at(phases.divide(game.moves), card.ply)


def _tags(entries: list[tuple[Game, CardSpec]]) -> list[str]:
    """Tags from the games and cards of a note: each group is the union over its cards."""
    t: set[str] = set()
    for g, c in entries:
        t |= {
            f"phase::{card_phase(g, c)}",
            f"site::{g.source}",
            f"color::{openings.color(g)}",
            f"result::{RESULT.get(g.user_result(), 'draw')}",
            "rated" if g.rated else "casual",
            f"opening::{openings.slug(openings.family(openings.classify(g.moves)[1]))}",
        }
        if g.time_class:
            t.add(f"time::{g.time_class}")
        if c.played.nag in SEVERITY:
            t.add(f"severity::{SEVERITY[c.played.nag]}")
        t |= {f"motif::{m}" for m in c.motifs}
        if not c.mine:
            t.add("opponent")  # cards from the opponent's mistakes
    return [f"{NOTE_TAG}::{x}" for x in sorted(t)]


def _clozes(ids: dict[str, int], cards: list[CardSpec]) -> str:
    numbers = sorted({ids[c.fen_key] for c in cards})
    return " ".join(f"{{{{c{n}::{n}::{n}}}}}" for n in numbers)


def puzzle_head(game: Game, card: CardSpec) -> str:
    """First line of a puzzle's Notes field: the phase and the game it comes from."""
    return " · ".join(p for p in (card_phase(game, card).capitalize(), _game_link(game)) if p)


def puzzle_notes(head: str, sections: list[str], links: list[str]) -> str:
    """The Notes field: `head`, then the stats of the games (one HTML section per card, see statsview.py), then the
    comments from similar positions."""
    out = ([head] if head else []) + ["".join(s for s in sections if s)]
    if links:
        out += ["<b>Similar positions</b>"] + [html.escape(x) for x in links]
    return "<br>".join(x for x in out if x)


def build_puzzle(
    game: Game,
    scores: list[Optional[Score]],
    first: CardSpec,
    second: Optional[CardSpec],
    notes: str,
    inaccuracy: float = Config.inaccuracy,
    nags: Optional[dict[int, int]] = None,
    lead: Optional[tuple[str, Choice]] = None,
) -> NoteSpec:
    """The note of one mistake: cloze 1 is the position before it, cloze 2 (when there is one) the position
    after it, where the best reply punishes it. Tags describe the first puzzle only.

    One game: the moves up to the mistake, then the mistake with the solution trees already grown under
    `first` and `second` (see follow.py): the punishment's answers are the moves that follow the mistake.
    """
    key = puzzle_key(first.fen_key, first.ply)
    ids = {first.fen_key: 1}
    cards = [first]
    if second is not None:
        ids[second.fen_key] = 2
        cards.append(second)
        second.played.tag = 2
        second.played.comment = _join(second.comment, second.played.comment)
        first.played.children = [second.played, *second.alternatives]
    return NoteSpec(
        key=key,
        pgn=_puzzle_pgn(game, scores, first, inaccuracy, nags, lead).strip(),
        cards=_clozes(ids, cards),
        notes=notes,
        tags=[NOTE_TAG, note_key_tag(key)] + _tags([(game, first)]),
        card_ids=ids,
        new_cards=len(cards),
    )

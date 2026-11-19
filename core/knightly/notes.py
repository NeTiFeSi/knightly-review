"""Write a game and its cards as a note in the Knightly Review PGN contract (see card/README.md)."""

from __future__ import annotations

import html
import io
import re
import time
from typing import Optional

import chess
import chess.pgn

from .cache import fen_key
from .engine.analyse import MATED
from .model import CardSpec, Game, NoteSpec, Score

KR_TAG = re.compile(r"\[%kr\s+(\d+)\s*\]")
NOTE_TAG = "knightly"


def note_key_tag(key: str) -> str:
    """Anki tag that finds a game's note again, e.g. knightly::game::lichess-abcd1234."""
    return f"{NOTE_TAG}::game::" + re.sub(r"[^A-Za-z0-9_-]", "-", key.replace(":", "-"))


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


def build(
    game: Game,
    scores: list[Optional[Score]],
    cards: list[CardSpec],
    existing_ids: Optional[dict[str, int]] = None,
) -> NoteSpec:
    """The note for `game` with `cards` tagged. Positions already in `existing_ids` keep their number."""
    ids = dict(existing_ids or {})
    nxt = max(ids.values(), default=0) + 1
    by_ply = {}
    for c in sorted(cards, key=lambda c: c.ply):
        if c.fen_key not in ids:
            ids[c.fen_key] = nxt
            nxt += 1
        by_ply[c.ply] = c
    new_cards = sum(1 for c in cards if c.fen_key not in (existing_ids or {}))

    pg = chess.pgn.Game()
    h = pg.headers
    h["Event"] = game.event or "?"
    h["Site"] = game.url or "?"
    h["Date"] = time.strftime("%Y.%m.%d", time.gmtime(game.end_ts)) if game.end_ts else "????.??.??"
    h["White"] = game.white or "?"
    h["Black"] = game.black or "?"
    h["Result"] = game.result
    if game.white_elo:
        h["WhiteElo"] = game.white_elo
    if game.black_elo:
        h["BlackElo"] = game.black_elo
    del h["Round"]

    node: chess.pgn.GameNode = pg
    for ply, u in enumerate(game.moves):
        card = by_ply.get(ply)
        parent = node
        node = parent.add_variation(chess.Move.from_uci(u))
        after = scores[ply + 1] if ply + 1 < len(scores) else None
        if card is None:
            node.comment = _eval(after)
            continue
        node.nags = {card.played.nag}
        node.comment = _join(_eval(card.played.score or after), f"[%kr {ids[card.fen_key]}]", card.comment)
        for alt in card.alternatives:
            v = parent.add_variation(chess.Move.from_uci(alt.move))
            v.nags = {alt.nag}
            v.comment = _eval(alt.score)
            for r in alt.reply:
                v = v.add_variation(chess.Move.from_uci(r))
                v.comment = _eval(alt.score)
    pgn = pg.accept(chess.pgn.StringExporter(headers=True, variations=True, comments=True))

    numbers = sorted({ids[c.fen_key] for c in cards})
    phases = sorted({c.phase for c in cards})
    if any(not c.mine for c in cards):
        phases.append("opponent")  # tag: this note has cards from the opponent's mistakes
    date = h["Date"].replace(".", "-")
    notes = f'<a href="{html.escape(game.url)}">{html.escape(game.white)} – {html.escape(game.black)}</a>, {date}' if game.url else ""
    return NoteSpec(
        key=game.key,
        pgn=pgn,
        cards=" ".join(f"{{{{c{n}::{n}::{n}}}}}" for n in numbers),
        notes=notes,
        tags=[NOTE_TAG, note_key_tag(game.key), f"{NOTE_TAG}::{game.source}"] + [f"{NOTE_TAG}::{p}" for p in phases],
        card_ids={k: v for k, v in ids.items()},
        new_cards=new_cards,
    )

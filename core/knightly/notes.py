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


GAME_TAG = f"{NOTE_TAG}::game::"
OPENING_TAG = f"{NOTE_TAG}::by-opening::"
_OPENING_TAG_RE = re.compile(re.escape(OPENING_TAG) + r"(white|black)-([a-z0-9-]+)::ply-(\d+)$", re.I)


def note_key_tag(key: str) -> str:
    """Anki tag that finds a note again.

    A game's note: knightly::game::lichess-abcd1234. An opening note (key
    opening:black:sicilian-defense:10): knightly::by-opening::black-sicilian-defense::ply-010.
    """
    if key.startswith("opening:"):
        _, color, slug, ply = key.split(":")
        return f"{OPENING_TAG}{color}-{slug}::ply-{int(ply):03d}"
    return GAME_TAG + re.sub(r"[^A-Za-z0-9_-]", "-", key.replace(":", "-"))


def key_from_tags(tags: list[str]) -> Optional[str]:
    """The note key that `note_key_tag` made, from a note's tags."""
    for t in tags:
        if t.lower().startswith(GAME_TAG):
            return t[len(GAME_TAG):].replace("-", ":", 1)
        m = _OPENING_TAG_RE.match(t)
        if m:
            return f"opening:{m[1].lower()}:{m[2].lower()}:{int(m[3])}"
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


def _assign_ids(cards: list[CardSpec], existing_ids: Optional[dict[str, int]]) -> tuple[dict[str, int], int]:
    """Cloze number per position: existing ones kept, new ones numbered on in ply order."""
    ids = dict(existing_ids or {})
    nxt = max(ids.values(), default=0) + 1
    for c in sorted(cards, key=lambda c: c.ply):
        if c.fen_key not in ids:
            ids[c.fen_key] = nxt
            nxt += 1
    new_cards = sum(1 for c in cards if c.fen_key not in (existing_ids or {}))
    return ids, new_cards


def _game_pgn(game: Game, scores: list[Optional[Score]], cards: list[CardSpec], ids: dict[str, int]) -> str:
    """One game with `cards` tagged."""
    by_ply = {c.ply: c for c in cards}
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
    return pg.accept(chess.pgn.StringExporter(headers=True, variations=True, comments=True))


def _date(game: Game, sep: str = "-") -> str:
    if not game.end_ts:
        return sep.join(["????", "??", "??"])
    return time.strftime(f"%Y{sep}%m{sep}%d", time.gmtime(game.end_ts))


def _game_link(game: Game) -> str:
    if not game.url:
        return ""
    return f'<a href="{html.escape(game.url)}">{html.escape(game.white)} – {html.escape(game.black)}</a>, {_date(game)}'


def _phase_tags(cards: list[CardSpec]) -> list[str]:
    phases = sorted({c.phase for c in cards})
    if any(not c.mine for c in cards):
        phases.append("opponent")  # tag: this note has cards from the opponent's mistakes
    return [f"{NOTE_TAG}::{p}" for p in phases]


def _clozes(ids: dict[str, int], cards: list[CardSpec]) -> str:
    numbers = sorted({ids[c.fen_key] for c in cards})
    return " ".join(f"{{{{c{n}::{n}::{n}}}}}" for n in numbers)


def build(
    game: Game,
    scores: list[Optional[Score]],
    cards: list[CardSpec],
    existing_ids: Optional[dict[str, int]] = None,
) -> NoteSpec:
    """The note for `game` with `cards` tagged. Positions already in `existing_ids` keep their number."""
    ids, new_cards = _assign_ids(cards, existing_ids)
    return NoteSpec(
        key=game.key,
        pgn=_game_pgn(game, scores, cards, ids),
        cards=_clozes(ids, cards),
        notes=_game_link(game),
        tags=[NOTE_TAG, note_key_tag(game.key), f"{NOTE_TAG}::{game.source}"] + _phase_tags(cards),
        card_ids=ids,
        new_cards=new_cards,
    )


def move_label(ply: int) -> str:
    """Ply index 9 → 'move 5…' (Black's 5th move); 8 → 'move 5'."""
    return f"move {ply // 2 + 1}" + ("…" if ply % 2 else "")


def _links_from_pgn(pgn: str) -> list[str]:
    out = []
    f = io.StringIO(pgn)
    while (h := chess.pgn.read_headers(f)) is not None:
        site = h.get("Site", "?")
        if site and site != "?":
            date = h.get("Date", "????.??.??").replace(".", "-")
            out.append(f'<a href="{html.escape(site)}">{html.escape(h.get("White", "?"))} – '
                       f'{html.escape(h.get("Black", "?"))}</a>, {date}')
    return out


def build_opening(
    key: str,
    label: str,
    ply: int,
    entries: list[tuple[Game, list[Optional[Score]], CardSpec]],
    existing_pgn: str = "",
) -> NoteSpec:
    """The note for one opening and ply: one game per card, each with its card tagged.

    `key` is the note key (see select.opening_note_key), `label` e.g. "Sicilian Defense (Black)".
    The games already in the note (`existing_pgn`) are kept as they are, with their
    numbers; the new games follow.
    """
    old_ids = card_ids(existing_pgn) if existing_pgn else {}
    cards = [c for _, _, c in entries]
    ids, new_cards = _assign_ids(cards, old_ids)
    entries = sorted(entries, key=lambda e: ids[e[2].fen_key])
    parts = [existing_pgn.strip()] if existing_pgn.strip() else []
    parts += [_game_pgn(g, s, [c], ids).strip() for g, s, c in entries]
    pgn = "\n\n".join(parts)
    head = f"{html.escape(label)}, {move_label(ply)} (ply {ply + 1})"
    color = key.split(":")[1]
    numbers = sorted(set(ids.values()))
    return NoteSpec(
        key=key,
        pgn=pgn,
        cards=" ".join(f"{{{{c{n}::{n}::{n}}}}}" for n in numbers),
        notes="<br>".join([head] + _links_from_pgn(pgn)),
        tags=[NOTE_TAG, note_key_tag(key), f"{NOTE_TAG}::{color}"]
        + sorted({f"{NOTE_TAG}::{g.source}" for g, _, _ in entries}) + _phase_tags(cards),
        card_ids=ids,
        new_cards=new_cards,
    )

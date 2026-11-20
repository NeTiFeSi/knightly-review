"""Filtered decks under the Knightly Review deck: the same cards, studied in another order or slice.

Only `anki` (pylib) imports, no Qt, so it can be tested against a temporary collection.

Each study deck is a filtered deck named `<deck>::<name>`. Filtered decks pull cards out of the main
deck when they are built and give them back when emptied. Notes are found by tag and not by deck, so
updates keep working while cards sit in a study deck. A card is in one filtered deck at a time, so
the add-on only creates them empty and the user rebuilds the one to study.
"""

from __future__ import annotations

from dataclasses import dataclass

from anki.collection import Collection
from anki.decks_pb2 import Deck
from anki.errors import FilteredDeckError

from .collection import MODEL
from .vendor.knightly import notes as kn

ORDER = Deck.Filtered.SearchTerm.Order
SEPARATOR = " - "
MATE_TAGS = "tag:knightly::motif::*mate*"  # back-rank-mate, smothered-mate, mate-in-2, ...

PIECES = ("pawn", "knight", "bishop", "rook", "queen", "king")


@dataclass(frozen=True)
class Preset:
    name: str  # the deck's own name, shown under the main deck
    tag: str  # Anki tag search
    order: int = ORDER.DUE  # DUE keeps the new-card order the update set


def _tag(path: str) -> str:
    return f"tag:{kn.NOTE_TAG}::{path}"


STATIC = [
    Preset("Severity - Blunders", _tag("severity::blunder")),
    Preset("Severity - Mistakes", _tag("severity::mistake")),
    Preset("Severity - Inaccuracies", _tag("severity::inaccuracy")),
    Preset("Phase - Opening", _tag("phase::opening")),
    Preset("Phase - Middlegame", _tag("phase::middlegame")),
    Preset("Phase - Endgame", _tag("phase::endgame")),
    Preset("Mate only", MATE_TAGS),
    Preset("Random mix", "", ORDER.RANDOM),
] + [Preset(f"Piece - {p.capitalize()}", _tag(f"piece::{p}")) for p in PIECES]

# (deck name prefix, tag prefix): one deck per value found, e.g. "Motif - fork" for knightly::motif::fork
DYNAMIC = [("Motif", "motif"), ("Opening", "opening"), ("Format", "time")]


def _pretty(value: str) -> str:
    return value.replace("-", " ").capitalize()


def _search(preset: Preset) -> str:
    return " ".join(p for p in (f'"note:{MODEL}"', preset.tag, "-is:suspended") if p)


def _count(col: Collection, tag: str) -> int:
    return len(col.find_notes(f'"note:{MODEL}" {tag}'))


def presets(col: Collection, min_cards: int = 5) -> list[Preset]:
    """The static presets that match some card, and one per motif, opening and time control seen."""
    out = [p for p in STATIC if p.name == "Random mix" or _count(col, p.tag)]
    seen = {t.lower() for t in col.tags.all()}
    for label, key in DYNAMIC:
        prefix = f"{kn.NOTE_TAG}::{key}::"
        for t in sorted(seen):
            if t.startswith(prefix) and "::" not in t[len(prefix):]:
                value = t[len(prefix):]
                tag = f"tag:{t}"
                if min_cards <= 1 or _count(col, tag) >= min_cards:
                    out.append(Preset(f"{label}{SEPARATOR}{_pretty(value)}", tag))
    return out


def sync(col: Collection, deck: str, limit: int = 100, min_cards: int = 5) -> list[str]:
    """Create the study decks that are missing. Returns the names created.

    A new study deck is built and emptied at once, so it exists but holds no cards: the cards stay
    in the main deck, in the order the update set, until the user rebuilds a study deck (the Rebuild
    button on its overview). Overlapping decks then never fight over cards at creation. Existing study
    decks are not touched, and neither is a normal deck that has a study deck's name. Study decks no
    longer wanted (a motif that went below `min_cards`) are removed when empty and under `deck`.
    """
    parent = deck or MODEL
    col.decks.id(parent)
    wanted = presets(col, min_cards)
    created: list[str] = []
    for p in wanted:
        name = f"{parent}::{p.name}"
        if col.decks.id_for_name(name):
            continue  # ours from an earlier run, or the user's own deck
        cfg = col.sched.get_or_create_filtered_deck(deck_id=0)
        cfg.name = name
        cfg.config.reschedule = True
        del cfg.config.search_terms[:]
        term = cfg.config.search_terms.add()
        term.search = _search(p)
        term.limit = limit
        term.order = p.order
        try:
            res = col.sched.add_or_update_filtered_deck(cfg)  # builds it too
        except FilteredDeckError:
            continue  # every matching card is in another filtered deck
        col.sched.empty_filtered_deck(res.id)
        created.append(p.name)
    names = {f"{parent}::{p.name}" for p in wanted}
    for d in col.decks.all_names_and_ids():
        if d.name.startswith(parent + "::") and d.name not in names and _ours(d.name[len(parent) + 2:]):
            if col.decks.get(d.id)["dyn"] and not col.decks.card_count([d.id], include_subdecks=False):
                col.decks.remove([d.id])
    return created


def _ours(name: str) -> bool:
    return name in {p.name for p in STATIC} or any(name.startswith(label + SEPARATOR) for label, _ in DYNAMIC)

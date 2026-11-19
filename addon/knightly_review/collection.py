"""Everything that touches the Anki collection: note type, media, and the generated notes.

Only `anki` (pylib) imports, no Qt, so it can be tested against a temporary collection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anki.collection import Collection, OpChanges
from anki.consts import MODEL_CLOZE

from .config_block import BLOCK_RE
from .vendor.knightly import notes as kn
from .vendor.knightly.model import NoteSpec

MODEL = "Knightly Review"
FIELDS = ("PGN", "Cards", "Notes")
BUNDLE = "_knightly.js"
MEDIA = (BUNDLE, "_kr_stockfish.js", "_kr_stockfish.wasm")  # card bundle + built-in engine for other clients
CARD_DIR = Path(__file__).resolve().parent / "card"
GAME_TAG_PREFIX = f"{kn.NOTE_TAG}::game::"


def _assets(card_dir: Path = CARD_DIR) -> dict[str, Any]:
    return {
        "media": {name: (card_dir / name).read_bytes() for name in MEDIA if (card_dir / name).exists()},
        "front": (card_dir / "front.html").read_text(encoding="utf-8"),
        "back": (card_dir / "back.html").read_text(encoding="utf-8"),
        "css": (card_dir / "style.css").read_text(encoding="utf-8"),
    }


OLD_DEFAULT_ACCEPT = "acceptNags: [1, 3, 5],"
NEW_DEFAULT_ACCEPT = "acceptNags: [1, 3, 5, 8],"  # $8 (singular move) became an accepted answer


def _keep_config(fresh: str, current: str) -> str:
    """The new template text, with the user's KR_CONFIG block carried over.

    A block still on the old default acceptNags gets the new default, so moves the
    generator marks as singular ($8) keep counting as correct.
    """
    kept = BLOCK_RE.search(current or "")
    if not kept:
        return fresh
    block = kept.group(0).replace(OLD_DEFAULT_ACCEPT, NEW_DEFAULT_ACCEPT)
    return BLOCK_RE.sub(lambda _m: block, fresh)


def ensure_installed(col: Collection, card_dir: Path = CARD_DIR) -> list[str]:
    """Create or update the note type and the card bundle. Returns what changed, for the log."""
    a = _assets(card_dir)
    changes = []

    media = col.media
    if BUNDLE not in a["media"]:
        raise RuntimeError(f"{BUNDLE} is missing from the add-on")
    for fname, data in a["media"].items():
        path = Path(media.dir()) / fname
        if path.exists() and path.read_bytes() == data:
            continue
        if path.exists():
            media.trash_files([fname])  # write_data would otherwise rename instead of replacing
        name = media.write_data(fname, data)
        if name != fname:
            raise RuntimeError(f"could not write {fname} (Anki saved it as {name})")
        changes.append(f"{fname} updated")

    mm = col.models
    nt = mm.by_name(MODEL)
    if nt is None:
        nt = mm.new(MODEL)
        nt["type"] = MODEL_CLOZE
        for f in FIELDS:
            mm.add_field(nt, mm.new_field(f))
        t = mm.new_template("Puzzle")
        t["qfmt"], t["afmt"] = a["front"], a["back"]
        mm.add_template(nt, t)
        nt["css"] = a["css"]
        mm.add_dict(nt)
        changes.append("note type created")
        return changes

    names = [f["name"] for f in nt["flds"]]
    if names[: len(FIELDS)] != list(FIELDS) or nt.get("type") != MODEL_CLOZE:
        raise RuntimeError(f'the "{MODEL}" note type has unexpected fields {names}; rename it and try again')
    dirty = False
    for t in nt["tmpls"]:
        for side, fresh in (("qfmt", a["front"]), ("afmt", a["back"])):
            new = _keep_config(fresh, t[side])
            if new != t[side]:
                t[side] = new
                dirty = True
    if nt["css"] != a["css"]:
        nt["css"] = a["css"]
        dirty = True
    if dirty:
        mm.update_dict(nt)
        changes.append("note type updated (your card settings kept)")
    return changes


def key_from_tags(tags: list[str]) -> str | None:
    for t in tags:
        if t.lower().startswith(GAME_TAG_PREFIX):
            return t[len(GAME_TAG_PREFIX):].replace("-", ":", 1)
    return None


def existing_notes(col: Collection) -> dict[str, str]:
    """Generated notes in the collection: game key → PGN."""
    out: dict[str, str] = {}
    for nid in col.find_notes(f'"note:{MODEL}" "tag:{GAME_TAG_PREFIX}*"'):
        note = col.get_note(nid)
        key = key_from_tags(note.tags)
        if key:
            out[key] = kn.field_to_pgn(note["PGN"])
    return out


def apply_notes(col: Collection, specs: list[NoteSpec], deck: str) -> OpChanges:
    """Add new notes and update changed ones, as one undoable step."""
    nt = col.models.by_name(MODEL)
    if nt is None:
        raise RuntimeError(f'note type "{MODEL}" is missing')
    did = col.decks.id(deck or MODEL)
    pos = col.add_custom_undo_entry("Knightly Review: new cards")
    for spec in specs:
        nids = col.find_notes(f'"note:{MODEL}" "tag:{kn.note_key_tag(spec.key)}"')
        note = col.get_note(nids[0]) if nids else col.new_note(nt)
        note["PGN"] = kn.pgn_to_field(spec.pgn)
        note["Cards"] = spec.cards
        note["Notes"] = spec.notes
        for t in spec.tags:
            if not note.has_tag(t):
                note.add_tag(t)
        if nids:
            col.update_note(note)
        else:
            col.add_note(note, did)
    return col.merge_undo_entries(pos)


def count_notes(col: Collection) -> int:
    return len(col.find_notes(f'"note:{MODEL}" "tag:{kn.NOTE_TAG}"'))


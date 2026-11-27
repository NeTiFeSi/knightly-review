#!/usr/bin/env python3
"""Build an .apkg with the Knightly Review note type and one note per PGN file.

Usage: python tools/make_sample_apkg.py [PGN_FILE ...] [-o OUT.apkg]
Requires `npm run build` first and Anki's own Python package (`pip install anki`,
or run it with the Python that Anki itself uses).
"""

import argparse
import hashlib
import html
import re
import shutil
import tempfile
from pathlib import Path

from anki.collection import Collection, DeckIdLimit, ExportAnkiPackageOptions
from anki.consts import MODEL_CLOZE

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

MODEL = "Knightly Review"
MODEL_ID = 1730496210  # keep stable: importing a newer build then updates the same note type
DECK = "Knightly Review (sample)"

KR_TAG = re.compile(r"\[%kr\s+(\d+)\s*\]")


def cards_field(pgn: str) -> str:
    ids = sorted({int(n) for n in KR_TAG.findall(pgn)})
    if not ids:
        raise SystemExit("PGN has no [%kr N] tags")
    return " ".join(f"{{{{c{n}::{n}::{n}}}}}" for n in ids)


def guid_for(name: str) -> str:
    """Stable note id across builds, so re-importing updates the note instead of duplicating it."""
    return "kr-sample-" + hashlib.sha256(name.encode()).hexdigest()[:16]


def add_note_type(path: str) -> None:
    """Create the note type, then give it the fixed id (Anki assigns a random one)."""
    col = Collection(path)
    try:
        t = DIST / "templates"
        mm = col.models
        nt = mm.new(MODEL)
        nt["type"] = MODEL_CLOZE
        for i, f in enumerate(("PGN", "Cards", "Notes", "Visits")):
            fld = mm.new_field(f)
            fld["id"] = MODEL_ID * 10 + i  # fixed ids: the importer then sees the same note type
            mm.add_field(nt, fld)
        tmpl = mm.new_template("Puzzle")
        tmpl["id"] = MODEL_ID * 10 + 9
        tmpl["qfmt"] = (t / "front.html").read_text()
        tmpl["afmt"] = (t / "back.html").read_text()
        mm.add_template(nt, tmpl)
        nt["css"] = (t / "style.css").read_text()
        old = mm.add_dict(nt).id
        for table in ("fields", "templates"):
            col.db.execute(f"update {table} set ntid=? where ntid=?", MODEL_ID, old)
        col.db.execute("update notetypes set id=? where id=?", MODEL_ID, old)
    finally:
        col.close()  # reopen below, so no cached copy of the old id survives


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pgn", nargs="*", default=[str(ROOT / "dev" / "samples.pgn")])
    ap.add_argument("-o", "--out", default=str(DIST / "knightly-sample.apkg"))
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp())
    try:
        path = str(tmp / "collection.anki2")
        add_note_type(path)
        col = Collection(path)
        try:
            nt = col.models.get(MODEL_ID)
            assert nt and nt["name"] == MODEL
            did = col.decks.id(DECK)
            col.media.write_data("_knightly.js", (DIST / "_knightly.js").read_bytes())
            for p in args.pgn:
                pgn = Path(p).read_text().strip()
                note = col.new_note(nt)
                note.guid = guid_for(Path(p).name)
                note["PGN"] = html.escape(pgn, quote=False).replace("\n", "<br>")
                note["Cards"] = cards_field(pgn)
                col.add_note(note, did)
            out = str(Path(args.out).resolve())
            col.export_anki_package(
                out_path=out,
                options=ExportAnkiPackageOptions(
                    with_scheduling=False, with_deck_configs=False, with_media=True, legacy=True
                ),
                limit=DeckIdLimit(did),
            )
        finally:
            col.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("wrote", args.out)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build an .apkg with the Knightly Review note type and one note per PGN file.

Usage: python tools/make_sample_apkg.py [PGN_FILE ...] [-o OUT.apkg]
Requires `npm run build` first and `pip install genanki`.
"""
import argparse
import html
import re
from pathlib import Path

import genanki

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

MODEL_ID = 1730496210  # keep stable: Anki identifies the note type by this id
DECK_ID = 1730496211

KR_TAG = re.compile(r"\[%kr\s+(\d+)\s*\]")


def model() -> genanki.Model:
    t = DIST / "templates"
    return genanki.Model(
        MODEL_ID,
        "Knightly Review",
        model_type=genanki.Model.CLOZE,
        fields=[{"name": "PGN"}, {"name": "Cards"}, {"name": "Notes"}],
        templates=[{
            "name": "Puzzle",
            "qfmt": (t / "front.html").read_text(),
            "afmt": (t / "back.html").read_text(),
        }],
        css=(t / "style.css").read_text(),
    )


def cards_field(pgn: str) -> str:
    ids = sorted({int(n) for n in KR_TAG.findall(pgn)})
    if not ids:
        raise SystemExit("PGN has no [%kr N] tags")
    return " ".join(f"{{{{c{n}::{n}::{n}}}}}" for n in ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pgn", nargs="*", default=[str(ROOT / "dev" / "samples.pgn")])
    ap.add_argument("-o", "--out", default=str(DIST / "knightly-sample.apkg"))
    args = ap.parse_args()

    m = model()
    deck = genanki.Deck(DECK_ID, "Knightly Review (sample)")
    for path in args.pgn:
        pgn = Path(path).read_text().strip()
        deck.add_note(genanki.Note(
            model=m,
            fields=[html.escape(pgn, quote=False).replace("\n", "<br>"), cards_field(pgn), ""],
        ))
    pkg = genanki.Package(deck)
    pkg.media_files = [str(DIST / "_knightly.js")]
    pkg.write_to_file(args.out)
    print("wrote", args.out)


if __name__ == "__main__":
    main()

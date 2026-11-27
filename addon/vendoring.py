"""Copy pure-Python packages into the add-on, rewriting their imports to be relative.

Anki installs AnkiWeb add-ons under a numeric folder name, and every add-on
shares one `sys.modules`, so a plain `import chess` could pick up another
add-on's copy (whichever loads first wins). Rewriting `import chess` to
`from .. import chess` (with the right number of dots) makes each bundled
package resolve inside our own `vendor/` folder, with no `sys.path` changes.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# `import chess`, `import chess.pgn`, `from chess import X`, `from chess.engine import Y`
_IMPORT = re.compile(r"^(?P<indent>[ \t]*)import (?P<mod>[\w.]+)[ \t]*(?P<rest>(#.*)?)$", re.MULTILINE)
_FROM = re.compile(r"^(?P<indent>[ \t]*)from (?P<mod>[\w.]+) import ", re.MULTILINE)


def rewrite_imports(source: str, names: set[str], depth: int) -> str:
    """Make absolute imports of `names` relative. `depth`: package levels between vendor/ and the module."""
    dots = "." * (depth + 1)

    def imp(m: re.Match) -> str:
        mod = m["mod"]
        top, _, sub = mod.partition(".")
        if top not in names:
            return m[0]
        line = f"{m['indent']}from {dots} import {top}"
        if sub:  # keep `top.sub` usable: importing the submodule sets it as an attribute
            line += f"; from {dots}{top} import {sub.split('.')[0]} as _kr_{sub.replace('.', '_')}"
        return line + (f"  {m['rest']}" if m["rest"] else "")

    def frm(m: re.Match) -> str:
        top = m["mod"].split(".")[0]
        if top not in names:
            return m[0]
        return f"{m['indent']}from {dots}{m['mod']} import "

    return _FROM.sub(frm, _IMPORT.sub(imp, source))


def vendor_tree(src: Path, dest: Path, names: set[str]) -> None:
    """Copy package folder `src` to `dest` (inside vendor/), rewriting imports of `names`."""
    if dest.exists():
        shutil.rmtree(dest)
    for f in sorted(src.rglob("*")):
        if "__pycache__" in f.parts or f.is_dir():
            continue
        rel = f.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if f.suffix == ".py":
            depth = len(rel.parts)  # vendor/<pkg>/<…>/mod.py → number of packages above mod
            out.write_text(rewrite_imports(f.read_text(encoding="utf-8"), names, depth), encoding="utf-8")
        elif f.suffix in (".typed", ".tsv"):  # .tsv: data files (the opening table)
            shutil.copy2(f, out)

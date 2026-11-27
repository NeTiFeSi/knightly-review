# Third-party notices

Knightly Review is licensed under the GNU Affero General Public License v3 or later (see `LICENSE`). It includes,
bundles or downloads the following.

## Code ported into this repository

### lichess-puzzler tagger (AGPL-3.0-or-later)

`core/knightly/cook.py` and `core/knightly/cookutil.py` are ported from
[lichess-puzzler](https://github.com/ornicar/lichess-puzzler) `tagger/cook.py` and `tagger/util.py`
(Copyright (C) the lichess-puzzler authors, AGPL-3.0-or-later). They tag a puzzle with Lichess's puzzle themes.

They are not a verbatim copy. The changes from upstream are:

- Imports, a local `Puzzle` class and no logging setup (the original port).
- Formatted with `ruff format` at 120 columns, so line breaks and parentheses differ from upstream.
- Style fixes required by ruff, with no change to what the code does:
  - imports sorted and merged, and `List[...]`/`Optional[...]` written as `list[...]`/`X | None` (Python 3.11+);
  - nested `if` statements merged into one condition (`SIM102`);
  - `True if x else False` and `if c: return True; return False` returned directly (`SIM210`, `SIM103`);
  - `not a in b` written `a not in b`, and a list comprehension inside `all()` made a generator (`C419`).

If a diff against upstream looks large, that is the formatting. When porting upstream changes, run
`ruff format` on the result and compare behaviour with `core/tests/test_motifs.py`.

## Libraries bundled into the add-on

| Component | Licence | Where |
| --- | --- | --- |
| [python-chess](https://github.com/niklasf/python-chess) 1.11.2 | GPL-3.0-or-later | vendored into the `.ankiaddon` by `addon/build.py` |
| [chessground](https://github.com/lichess-org/chessground) 9.x | GPL-3.0-or-later | bundled into `_knightly.js` |
| [chessops](https://github.com/niklasf/chessops) 0.15.x | GPL-3.0-or-later | bundled into `_knightly.js` |

`requests` (Apache-2.0) is not bundled: Anki ships it.

## Data and artwork

- **Openings table** (`core/knightly/data/openings.tsv`): Lichess
  [chess-openings](https://github.com/lichess-org/chess-openings), CC0 1.0 (public domain).
- **Piece set**: "cburnett" by Colin M. L. Burnett, as shipped in chessground
  (`chessground.cburnett.css`), GPL-2.0-or-later. It is embedded in `_knightly.js`.

## Downloaded or installed by the user, not distributed here

- [Stockfish](https://stockfishchess.org/) (GPL-3.0). The add-on can download the official build from the Stockfish
  GitHub releases into its own folder, or use one you installed. Source: <https://github.com/official-stockfish/Stockfish>.

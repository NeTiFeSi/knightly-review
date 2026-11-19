# knightly (core)

The part that knows chess: fetch games, analyse them, choose mistakes, and write notes in the card's PGN format. It has no Anki imports, so it runs inside the add-on (bundled), from the command line, and later in a helper process or on a server.

Dependencies: `python-chess` (bundled into the add-on) and `requests` (Anki ships it). Python 3.10+.

```sh
uv venv && uv pip install -e .        # or: python -m venv .venv && pip install -e .
python -m unittest discover -s tests  # no network or engine needed
python -m knightly engines            # Stockfish / Lc0 found on this computer
python -m knightly install-options    # how this OS's package manager installs Stockfish
python -m knightly download-stockfish DIR
python -m knightly update --lichess USER --chesscom USER --max-games 5 --max-cards 3 --out notes.json
#   --depth 20 (Stockfish depth), --stockfish PATH, --lc0 PATH (also run Lc0, in parallel)
```

| Module | Does |
|---|---|
| `sources/lichess.py`, `sources/chesscom.py` | Download new games into the cache, within each site's limits |
| `sources/lichess_login.py` | "Log in with Lichess" (OAuth PKCE, loopback redirect) |
| `cache.py` | SQLite (WAL) cache: games, evals, HTTP validators, cursors. Safe across threads and processes |
| `engine/discover.py`, `engine/install.py` | Find Stockfish and Lc0 (no other engines are supported); package-manager commands; official Stockfish download |
| `engine/analyse.py` | Stockfish (fixed depth) and optional Lc0 (fixed time) in parallel, always with WDL; Lc0's answer is used except in mate positions; cached per engine |
| `select.py` | Win-rate loss (engine WDL), phase, recurrence; good-move and sacrifice tests, NAGs |
| `notes.py` | PGN with `[%kr N]` tags and stable card numbers |
| `jobs.py` | `run_update()`: the single entry point the add-on calls (pauses while `gate` is cleared) |
| `live.py` | Answers a card's live-play questions (best lines, or specific moves only) |

`tests/fixtures/generated_note.pgn` is a golden file that the card's own tests also parse (`card/test/generated.test.ts`). After a deliberate format change, regenerate it with `UPDATE_GOLDEN=1 python -m unittest discover -s tests` and run `npm test` in `card/`.

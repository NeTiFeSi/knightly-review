# knightly (core)

The part that knows chess: fetch games, analyse them, choose mistakes, and write notes in the card's PGN format. It has no Anki imports, so it runs inside the add-on (bundled), from the command line, and later in a helper process or on a server.

Dependencies: `python-chess` (bundled into the add-on) and `requests` (Anki ships it). Python 3.10+.

```sh
uv venv && uv pip install -e .        # or: python -m venv .venv && pip install -e .
python -m unittest discover -s tests  # no network or engine needed
python -m knightly engines            # Stockfish / Lc0 found on this computer
python -m knightly install-options    # how this OS's package manager installs Stockfish
python -m knightly download-stockfish DIR
python -m knightly update --lichess USER --chesscom USER --minutes 5 --max-cards 3 --out notes.json
#   --depth 20 (Stockfish depth), --stockfish PATH, --lc0 PATH (also run Lc0, after Stockfish)
```

| Module | Does |
|---|---|
| `sources/lichess.py`, `sources/chesscom.py` | Download new games into the cache, within each site's limits |
| `sources/lichess_login.py` | "Log in with Lichess" (OAuth PKCE, loopback redirect) |
| `cache.py` | SQLite (WAL) cache: games, evals, HTTP validators, cursors. Safe across threads and processes |
| `engine/discover.py`, `engine/install.py` | Find Stockfish and Lc0 (no other engines are supported); package-manager commands; official Stockfish download |
| `engine/analyse.py` | Stockfish (fixed depth), then optional Lc0 for as long as Stockfish took, always with WDL; Lc0's answer is used except in mate positions; cached per engine |
| `deepen.py` | Analysis by increasing depth: every position at depth 1 (most-seen first), then possible mistakes deeper one depth at a time, then the full setup that makes the card; the rest is deepened when no candidates are left. Stops at enough cards or the time budget |
| `select.py` | Win-rate loss (engine WDL); which moves become cards, in which order (game by game, or by opening and ply); good-move and sacrifice tests, NAGs |
| `motifs.py` | Tactical motifs of a puzzle (fork, pin, skewer, back-rank mate, mate in N, …) from the best move and the engine's line; the card's main motif picks its note in `note_grouping="motif"` |
| `phases.py` | Opening, middlegame or endgame: a port of Lichess's game divider (scalachess `Divider`) |
| `openings.py` | Names a game's opening (family) and ranks openings by points lost (4 per loss, 2 per draw, 1 per win), per colour |
| `notes.py` | PGN with `[%kr N]` tags and stable card numbers; one note per game and phase, or per opening and ply; the notes' tags (one game per card) |
| `jobs.py` | `run_update()`: the single entry point the add-on calls (pauses while `gate` is cleared) |
| `live.py` | Answers a card's live-play questions (best lines, or specific moves only) |

`data/openings.tsv` is Lichess's [chess-openings](https://github.com/lichess-org/chess-openings) table (CC0), stored as the position each named line reaches (FEN without move counters), so transpositions are recognised.

`tests/fixtures/generated_note.pgn` is a golden file that the card's own tests also parse (`card/test/generated.test.ts`). After a deliberate format change, regenerate it with `UPDATE_GOLDEN=1 python -m unittest discover -s tests` and run `npm test` in `card/`.

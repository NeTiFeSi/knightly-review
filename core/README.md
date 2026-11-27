# knightly (core)

The part that knows chess: fetch games, analyse them, choose mistakes, and write notes in the card's PGN format. It has no Anki imports, so it runs inside the add-on (bundled), from the command line, and later in a helper process or on a server.

Dependencies: `python-chess` (bundled into the add-on) and `requests` (Anki ships it). Python 3.11+.

```sh
uv venv && uv pip install -e .        # or: python -m venv .venv && pip install -e .
python -m unittest discover -s tests  # no network or engine needed
python -m knightly engines            # Stockfish found on this computer
python -m knightly install-options    # how this OS's package manager installs Stockfish
python -m knightly download-stockfish DIR
python -m knightly update --lichess USER --chesscom USER --minutes 5 --max-cards 3 --out notes.json
#   --depth 18 (Stockfish depth), --stockfish PATH
```

| Module | Does |
|---|---|
| `sources/lichess.py`, `sources/chesscom.py` | Download new games into the cache, within each site's limits |
| `sources/lichess_login.py` | "Log in with Lichess" (OAuth PKCE, loopback redirect) |
| `cache.py` | SQLite (WAL) cache: games, evals, HTTP validators, cursors. Safe across threads and processes |
| `engine/discover.py`, `engine/install.py` | Find Stockfish (no other engine is supported); package-manager commands; official Stockfish download |
| `engine/analyse.py` | Stockfish at fixed depths, always with WDL: a cheap scan (best move and the moves played, fewest lines) and the full search (one line per legal move, MultiPV = every move); both cached per position |
| `pipeline.py` | The one way puzzles are made: ply by ply, every move of every position at a low depth, the biggest suspected mistakes promoted through 2–3 depth steps to the configured one, then confirmed (`select.build_card`) or ruled out; one note per mistake; similar positions and repeated moves update an existing note instead |
| `stats.py`, `similar.py`, `puzzles.py` | How often each move was played at a position and ply; "very similar" positions (same side to move, ≤2 squares differ) and the comments they add; the puzzle records kept in the cache (id = position + ply) |
| `select.py` | Win-rate loss (engine WDL); whether a position is a puzzle (`build_card`) and its punishment (`build_punish`); good-move and sacrifice tests, NAGs |
| `motifs.py`, `cook.py`, `cookutil.py` | Tactical motifs of a puzzle (Lichess puzzle themes: fork, pin, back rank mate, mate in N, …), tagged with lichess-puzzler's `cook.py` (ported, AGPL) from the best move and the engine's line |
| `phases.py` | Opening, middlegame or endgame: a port of Lichess's game divider (scalachess `Divider`) |
| `openings.py` | Names a game's opening (family), for the notes' tags |
| `notes.py` | PGN with `[%kr N]` tags: cloze 1 the mistake, cloze 2 its punishment; the Notes field; the notes' tags (from the first puzzle) |
| `progress.py` | `Tracker`: what a run is doing (stage with a growing total, per-engine searches with live depth, puzzles found, ply, time left), read from any thread; `describe()` formats it for the add-on window and the CLI (tqdm bars when installed) |
| `jobs.py` | `run_update()`: the single entry point the add-on calls (pauses while `gate` is cleared) |
| `follow.py` | The solution tree under a puzzle's answers: both sides' good moves followed until a stop (decided, drawn, a Stockfish mate, a small gap, game over), and the decision positions found on the way; each becomes a note (`pipeline.py`, deepest first) |

`data/openings.tsv` is Lichess's [chess-openings](https://github.com/lichess-org/chess-openings) table (CC0), stored as the position each named line reaches (FEN without move counters), so transpositions are recognised.

`tests/fixtures/generated_note.pgn` is a golden file that the card's own tests also parse (`card/test/generated.test.ts`). After a deliberate format change, regenerate it with `UPDATE_GOLDEN=1 python -m unittest discover -s tests` and run `npm test` in `card/`.

# knightly-review
Your nightly chess reps, made from your own games. knightly-review pulls your Lichess/Chess.com games, finds your mistakes with an engine, and turns the ones costing you the most wins into interactive Anki cards: openings, tactics and endgames.

## Install

You need [Anki](https://apps.ankiweb.net/) on a computer (Windows, macOS or Linux). Cards are made on the computer, and reach AnkiDroid, AnkiMobile and AnkiWeb through normal Anki sync.

1. Install the add-on: double-click `knightly_review.ankiaddon`, or in Anki use **Tools → Add-ons → Install from file…**. Restart Anki.
2. A welcome window opens. Enter your Lichess and/or Chess.com username.
3. Check that Stockfish was found (it's required). If it isn't installed, its window offers two ways to get it:

   | Your system | Install it yourself | Or let the add-on do it |
   |---|---|---|
   | Windows 10/11 | `winget install --id Stockfish.Stockfish -e` (the add-on can run it for you) | **Download Stockfish for me** |
   | macOS | `brew install stockfish` if you use Homebrew (the add-on can run it for you) | **Download Stockfish for me** |
   | Arch, Manjaro | `sudo pacman -S stockfish` | **Download Stockfish for me** |
   | Debian, Ubuntu, Mint | `sudo apt install stockfish` | **Download Stockfish for me** |
   | Fedora | `sudo dnf install stockfish` | **Download Stockfish for me** |
   | openSUSE | `sudo zypper install stockfish` | **Download Stockfish for me** |

   **Download Stockfish for me** fetches the official build from the Stockfish GitHub releases, checks it, and keeps it inside the add-on's folder. No admin rights needed.

   Stockfish searches every position to a fixed depth: 13 by default, which is quick. 20 or more gives better judgements if your computer can take it. Its hash table size (RAM) is in the same box.

   Optional: **Lc0** as a second engine (much faster with a graphics card; get it from [lczero.org](https://lczero.org/play/download/)). When it's on, Lc0 analyses each position after Stockfish, for as long as Stockfish took there. Lc0's judgement is used, except in mate positions, where Stockfish's is. Only Stockfish and Lc0 are supported.
4. Save. The first update fetches your recent games and analyses them in the background (a few minutes; Anki stays usable). New cards appear in the **Knightly Review** deck.

After that, updates run when Anki starts and every few hours while it's open. **Tools → Knightly Review → Update now** runs one right away. Lichess: logging in (optional, in Settings) makes downloads faster; your password stays on lichess.org. Chess.com refreshes its game lists only a few times a day, so new Chess.com games can take a few hours to show up.

## How it works

```
core/      Python package `knightly`: fetch games, analyse, pick mistakes, write notes (no Anki imports)
addon/     the Anki add-on: settings, scheduling, writing notes; bundles core/ and python-chess
card/      the interactive card (TypeScript), built into one media file
```

- Games come from the Lichess API (incremental, one request at a time) and the Chess.com archive API (each finished month downloaded once; the current month re-checked with ETags).
- Every position is analysed by Stockfish (fixed depth), and by Lc0 too when it's on (fixed time), always with win/draw/loss output (`UCI_ShowWDL`). "Win rate" everywhere means the engine's expected score: wins + draws / 2. Evals are cached in a SQLite file, shared by threads and processes.
- A move becomes a card when it loses at least 10 % win rate. This is deliberate: the engine's WDL treats a position about 2 pawns up or down as already decided, so mistakes there barely register. Cards come from the moments where the result was still in play. Mistakes in games you didn't win, and positions you reach again and again, rank first. Each pick is checked again with a deeper multi-line analysis, which grades every candidate move (`!!`, `□`, `!?`, `!`, `?!`, `?`, `??`; see [card/README.md](card/README.md)) and gives the accepted alternatives on the back of the card.
- One note per game and phase (opening, middlegame, endgame, split as Lichess does), one card per mistake. Notes are tagged by phase, site, colour, time control, result, opening and how bad the mistake was (`knightly::…`). Card numbers stay stable when a game gets more cards later, so review history is kept. Optionally (off by default), your opponents' mistakes become cards too, played from their side.
- Optionally (off by default; ⚙ on any card), cards don't stop after one move: past the stored first move and reply, a live engine keeps the position going until mate, a draw or a quiet position. On desktop that's your Stockfish/Lc0 through the add-on; on phones and AnkiWeb, a small built-in Stockfish that syncs with the cards (1.8 MB).

The add-on bundles only pure-Python code (python-chess and the core), so one file installs on every OS. Heavier work planned for later (personal engines that need ML libraries) will run in a separate helper process with its own Python, not inside Anki.

See [card/README.md](card/README.md) for the card and its PGN format, [addon/README.md](addon/README.md) for building the add-on, and [core/README.md](core/README.md) for the command line.

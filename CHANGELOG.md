# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **"The game is decided" on equal positions.** A game now counts as decided only when after every legal move the
  same side wins more than half the time, by Stockfish's win/draw/loss estimate. Before, an expected score just
  under or over 50 % was enough, which cut solution trees short, dropped real mistakes and left out punishment
  cards.
- **Notes made before this fix are repaired** by the next update, before any new puzzles: they are edited in place
  (your review history stays), and missing punishment cards and positions are added, within the usual limit of new
  cards per update. No card is removed. Mistakes that were wrongly dropped are looked at again. Left as they are:
  notes whose game is older than the history setting, notes made on another computer (this one has no record of
  them), and notes that would lose a card under your current settings.

## [0.2.0] - 2026-11-27

### Changed

- **Analysis is a cheap scan, then a full search of the suspects.** Every position first gets a scan at a fixed low
  depth (10) with the fewest lines that can measure a loss: one line for the best move, and each move the games
  played searched on its own. Only the positions where a played move looks worse than half of `min_loss` get the
  full search, to the configured depth with one line per legal move (Stockfish's MultiPV set to the number of legal
  moves). With a single line, Stockfish prunes the moves it thinks are worse and only bounds their scores;
  searching them all gives every move an exact score from the same search, so a move's loss against the best move is
  comparable. This replaces the old ladder of increasing depths (5, 9, 13). On a 10-thread desktop a scan takes
  about 0.02 s, a full search about 4 s at depth 13 and about 20 s at depth 16.
- **Grades on the game's other moves** (`!!`, `□`, `!?`, `?!`, `?`, `??`) come from a cheap look (the three best lines
  and the move played, at the scan depth), or from the full search where there already is one.
- The analysis cache is rebuilt on the first update (its searches are of a different kind now); games are kept.
- The progress window shows the position being analysed and one Stockfish row.

### Removed

- **Lc0.** It overrode Stockfish's judgement, but on 5 % of moves each engine said "mistake" where the other said
  "good", with no sign that Lc0 was the one that was right. It needed a separate install and added a second engine
  path, settings and analysis time. The `use_lc0` and `lc0_path` settings and the `--lc0` option are gone; old
  settings are ignored.

### Fixed

- The settings window no longer says "20+ recommended" next to the depth.
- The README's grade thresholds match the code (Good move 1 %, `?!` from 5 %).
- The README pictures show the current windows and cards, including the explorer-style stats.

## [0.1.0] - 2026-11-26

First public release.

### Added

- **Games from Lichess and Chess.com.** Lichess downloads are incremental (and faster when you log in); Chess.com
  months are downloaded once, and the current month is re-checked.
- **Engine analysis.** Stockfish judges every position at a fixed depth (13 by default, increasing depth for the
  biggest suspected mistakes first). Lc0 can run as an optional second engine. Judgements use the engine's win rate
  from its win/draw/loss output, and are cached in SQLite. The add-on finds installed engines and can download the
  official Stockfish build for you.
- **Puzzles from your mistakes.** A move that loses more than 1 % of win rate becomes a puzzle. Each pick is checked
  with a multi-line analysis that grades every candidate move and keeps the accepted alternatives; forced mates only
  accept moves that keep the mate. One note per position, with up to two cards: find the move, and punish the mistake.
  Puzzles are added to Anki as soon as they are built, in one undoable step.
- **Tags** for phase, site, colour, time control, result, opening, severity and tactical motif (motifs come from
  lichess-puzzler's tagger), plus opening-explorer-style statistics from your own games in the Notes field.
- **An interactive card** with a chessground board: hints, move-grade badges and NAGs, drawing arrows with the right
  mouse button, position export (copy FEN), optional timed puzzles, and a settings panel on the card whose changes
  sync to your other devices through the note type. Tested on Anki desktop and AnkiDroid 2.25 (both study screens).
- **A settings and progress window** with a live row per engine, rate and ETA. Updates run at startup and every few
  hours, skip work when nothing changed, and can keep analysing in the background.
- Opt-in modes: puzzles from your opponents' mistakes, ordering cards by opening or by motif, and the original ranked
  mode.
- A command-line interface (`python -m knightly`) and a sample deck (`knightly-sample.apkg`).

### Requirements

- Anki 25.07 or newer (the add-on needs Python 3.11 or newer; building from source also needs Python 3.11+ and
  Node.js).

[Unreleased]: https://github.com/NeTiFeSi/knightly-review/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/NeTiFeSi/knightly-review/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/NeTiFeSi/knightly-review/releases/tag/v0.1.0

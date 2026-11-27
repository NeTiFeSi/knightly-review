# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/NeTiFeSi/knightly-review/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/NeTiFeSi/knightly-review/releases/tag/v0.1.0

# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Several accounts, yours or other players'.** The settings now have a table of accounts: any number of Lichess
  and Chess.com usernames, each marked as yours or as another player's (a master, a rival, someone to compare
  with). Puzzles still come from your games only; other players' games are downloaded and exported, ready for
  later features. Your old Lichess and Chess.com usernames become accounts of yours.
- **Games from PGN files.** Add PGN files, or folders of them, in the settings. Games where one of your names
  plays become puzzles like your online games; a name listed as another player's is followed too.
- **PGN export.** Choose an export folder to have every account's games written there as PGN after each update,
  or use **Tools → Knightly Review → Export games…** once. Your games come annotated: each move the engine
  looked at gets its grade (?!, ?, ??) and the evaluation after it, which Lichess and ChessBase draw as a graph
  (Annotating page; on by default). The folder is written again as more moves are annotated.
- **An optional email for Chess.com**, sent with its downloads, as Chess.com asks of programs using its API.
- **Settings for the notes already in Anki:** keep them up to date (on by default; off leaves them as they are),
  replace their `knightly::` tags when they are written again (on by default), and suspend the notes this
  version can no longer keep up to date (off by default): notes from versions before puzzle keys, and notes made
  under older rules whose game is no longer among your games. Suspending is part of the update's undo step.
- **Statistics:** **Tools → Knightly Review → Statistics** shows how the engine judged your moves: inaccuracies,
  mistakes and blunders per 100 moves by month, by phase of the game and by time control; your most played
  openings with your score; results by colour; and how many puzzles are made and waiting. **Save as HTML…**
  keeps a copy to open in any browser (also `python -m knightly stats --addon <folder>`).
- **A companion process, `knightly daemon`,** to fetch games, annotate them and make puzzles while Anki is
  closed: `python -m knightly daemon --addon <your addons21 folder>/<Knightly Review's folder>`. It uses the
  add-on's settings (read again when they change), cache and Lichess login, and puts the notes it makes where
  the add-on picks them up as Anki opens. When Anki opens, the daemon stops after its current search and
  waits until Anki closes. Notes already in Anki are only changed or suspended by the add-on.

### Changed

- **Settings in pages, one per stage:** Games, Engine, Annotating, Candidates, Puzzles and Notes in Anki, each
  saying what that stage does. New there: which time controls count, rated games only, the scan depth, where an
  inaccuracy starts, and how much a move must lose to become a puzzle.
- **The progress window shows each stage:** its progress or its last result, and a button to run it now; plus the
  engine at work, and the log.
- **The work runs in stages, each on its own.** Fetching games, annotating your moves, choosing puzzle candidates,
  making cards and maintaining your notes each run on a thread of their own, as soon as the stage before them
  has something new, sharing Stockfish (making cards comes before annotating). An update no longer has to finish
  everything in one go: annotating carries on where it stopped, and each run of the card stage makes up to your
  "new cards per update". New cards are written into Anki every few seconds, each batch one undo step.
- **"Keep analysing after the update" is now "keep annotating in the background"**, at low priority. "Keep adding
  cards with no limit" is gone: cards are made up to your limit each time the card stage runs. Your setting
  carries over.
- Changing Stockfish's threads or memory no longer makes the next update analyse everything again. The first
  update after this version does analyse again once, mostly from the searches already saved.
- Only one program runs Stockfish at a time on the same data: an update from the command line waits while Anki's
  update is searching (and the other way round), instead of both loading the CPU at once.

### Fixed

- **Lichess games that were still being played during an update are no longer lost.** Each update moved past them,
  so a game in progress at that moment (most often a correspondence game) never reached your cards once it ended.
  Updates now keep track of such games and add them once they end. The next update downloads your history window from Lichess once more to pick up
  the games missed so far; games you already have are not added twice.
- **Undo after answering a card undoes the answer again.** The card's record of which replies it showed was saved
  as an Anki change of its own, so Ctrl+Z undid that record instead of your last answer. It is now saved when you
  answer the card, as part of the same undo step.
- **Wider game filters now bring in the games they used to leave out.** The time controls and "rated only" are
  applied while downloading, so adding a time control or turning "rated only" off had no effect on games already
  passed over. The next update now downloads your history window again from Lichess and Chess.com. Turning
  "rated only" on now also leaves out casual games that were downloaded before.
- **Castling no longer restarts the fifty-move count** on the card, so a line that reaches the fifty-move rule
  ends there as a draw.
- The Lichess login token file is now private to your user from the moment it is written.

## [0.2.1] - 2026-11-27

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

[Unreleased]: https://github.com/NeTiFeSi/knightly-review/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/NeTiFeSi/knightly-review/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/NeTiFeSi/knightly-review/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/NeTiFeSi/knightly-review/releases/tag/v0.1.0

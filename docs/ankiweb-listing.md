# AnkiWeb listing (text to paste at https://ankiweb.net/shared/addons)

Upload `knightly_review.ankiaddon` from the GitHub release. AnkiWeb gives the add-on a numeric code; once you have it,
add "Tools → Add-ons → Get Add-ons… → code `NNNNNNNN`" to the README's Install section.

**Title:** Knightly Review: chess puzzles from your own games

**Support page:** https://github.com/NeTiFeSi/knightly-review/issues

**Tags:** chess, lichess, chess.com, stockfish, puzzles

**Supported versions:** Anki 25.07 and newer (`min_point_version` in `manifest.json` is 250700).

## Description

Your nightly chess reps, made from your own games.

Knightly Review pulls your Lichess and Chess.com games, finds your mistakes with Stockfish, and turns the ones that
cost you the most wins into interactive Anki cards: openings, tactics and endgames. The card shows your game up to the
mistake and asks you to find a better move on a real board.

- **Your games, your mistakes.** Lichess and Chess.com, fetched in the background; new games are picked up
  automatically.
- **Judged by an engine.** Stockfish scores every legal move in every position, so accepted alternatives count, so accepted alternatives count
  and forced mates are respected.
- **Interactive cards.** Hints, move-grade badges, arrows you can draw, optional timed puzzles. Settings on the card
  sync to AnkiDroid, AnkiMobile and AnkiWeb through Anki's normal sync.
- **Organised.** Tags for phase, colour, time control, result, opening, severity and tactical motif.
- **Private.** Everything runs on your computer. The only network traffic is to Lichess, Chess.com and, if you ask for
  it, the Stockfish download.

Requires Stockfish. The add-on finds an installed one or can download the official build for you.

Free and open source (AGPL-3.0-or-later): https://github.com/NeTiFeSi/knightly-review

## Screenshots to upload (from `docs/img/`)

1. `front.png`: the card front
2. `wrong-move.png`: the answer after a wrong move
3. `addon-settings.png`: the settings window
4. `progress.png`: the progress window

## Before submitting

- Install the exact `.ankiaddon` you will upload into the isolated test profile and play a card.
- Anki shows the numeric folder name for AnkiWeb installs, so check that the card, settings and update all work from it.

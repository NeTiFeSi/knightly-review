# Card UI

The interactive chess board shown inside Anki for Knightly Review cards.
It uses chessground (the board) and chessops (rules, PGN parsing), and builds into one
media file, `_knightly.js`. That file includes the CSS and piece images.

```sh
npm install
npm run build      # dist/_knightly.js + dist/templates/{front,back}.html, style.css
npm test           # vitest
npm run typecheck
python tools/make_sample_apkg.py   # dist/knightly-sample.apkg (needs Anki's `anki` package: `pip install anki`)
```

**Dev harness:** run `python -m http.server -d card 8000`, then open <http://localhost:8000/dev/>.
It renders the real templates the way Anki does: cloze spans, `<br>` in fields and script execution.
It has a card picker, front/back buttons, night mode and a "rebuild page between sides" switch
that simulates AnkiMobile and AnkiWeb. Use the browser's device mode to check phone layouts.

## Note type `Knightly Review` (Cloze)

| Field | Content |
|---|---|
| `PGN` | One or more games |
| `Cards` | `{{c1::1::1}} {{c2::2::2}} …`, one cloze per `[%kr N]` tag. The hint and the answer must both be `N`. |
| `Notes` | Optional, shown on the back |

## PGN contract (what the generator must write)

```pgn
4. Nxe5?? { [%eval -1.5] [%kr 4] Greedy: Qg5 hits both e5 and g2. }
  (4. Nxd4! { [%eval 0.9] } exd4 5. O-O (5. c3!?) Nf6)
  (4. c3!? { [%eval 0.5] })
  (4. h3?! { [%eval 0.2] })
```

- **`[%kr N]`** goes in the comment of the *mainline* move that the game actually played. That move
  becomes card N, and the position before it is the puzzle. Keep N stable for a given position, because
  Anki keeps the scheduling history per cloze number.
- **Candidates** are the mainline move plus its variations. Each candidate gets a move NAG
  (`!!` `!` `!?` `?!` `?` `??` `□`, i.e. `$1`–`$6` and `$8`). By default `!!`, `□`, `!` and `!?` are
  accepted. The `acceptNags` setting in the templates' `KR_CONFIG` changes that. The generator uses
  them like this, where a *good move* loses at most 2 % (configurable) of the engine's expected
  score (WDL) against the best move:

  | NAG | Meaning |
  |---|---|
  | `!!` ($3) | the only good move, and a sacrifice |
  | `□` ($8, singular move) | the only good move, not a sacrifice |
  | `!?` ($5) | a good move that sacrifices material while another good move keeps more |
  | `!` ($1) | any other good move |
  | `?!` ($6) | not a good move; loses up to 10 % |
  | `?` ($2) | loses 10–20 % |
  | `??` ($4) | loses more than 20 % |

  In a **forced mate** (the best move mates by force), a good move is one that keeps a forced mate,
  however long: a move that gives the mate up is `?!` at best, even when the engine still calls the
  position won.

  A *sacrifice* ends 4 plies down the engine's line (carried on through captures and checks) at least
  2 points of material (1/3/3/5/9) below the alternative line that keeps the most material.
- **Continuations:** the user plays every own move of the chosen line, and the opponent's replies are
  played automatically. At a later user turn, the line's next move is accepted unless its NAG rejects
  it, and so is any sibling variation with an accepted NAG. A legal move that is not in the PGN counts
  as a failure ("not analysed").
- **Evals:** `[%eval 0.17]`, `[%eval #-3]` or `[%eval 0.17,22]`, from White's point of view, in the
  comment *after* a move. They describe the position reached by that move.
- **Arrows:** `[%cal Ge2e4]` and `[%csl Re4]` are drawn when that node is shown.
- **Playing on (live engine):** the PGN holds only the candidate moves and the reply. Where its line
  ends, a live engine can take over (setting `live`, off by default: turn it on in ⚙ or in `KR_CONFIG`). You keep playing until mate, a draw,
  a quiet position (two or more good moves, no check, no capture just made) or `liveMaxMoves`. Each of
  your moves must be within `goodMove` win-% of the engine's best (its WDL expected score); when the
  best move mates by force, your move must keep a forced mate instead (as fast as the best with
  `strictMate`), and the position is never "quiet" until the mate is played out. The
  opponent plays the engine's best reply. Live moves are shown in italics, and on the back too.
  - Desktop with the add-on: your Stockfish (and Lc0, if on), same rule as the analysis. Background
    analysis pauses while you review.
  - Everywhere else (AnkiDroid, AnkiMobile, AnkiWeb, desktop without the add-on): a built-in
    Stockfish (stockfish.js 19 "lite, single-threaded", GPL-3) shipped as the media files
    `_kr_stockfish.js` + `_kr_stockfish.wasm` (1.8 MB), capped at depth 18 / 1.5 s per search.
  - Without an engine, the puzzle ends where the PGN does.
- **Back side:** at the puzzle position only the accepted moves, the game move and your move are
  listed; "+ N more" shows the rejected ones.
- **What each side shows:**
  - Front: the mainline only. The NAG, comment, eval and arrows of every tagged move in the note are
    hidden, and so are the eval and arrows of the position before each one.
  - Back: the mainline plus this card's variations only. Other cards' tagged moves stay hidden.
  - The same game can appear in other notes with different tags.

- **Hints:** `[%hint text]` on the tagged move is shown as the first hint. The next hint circles the
  piece(s) to move. Hints are counted, and the back reports how many were used.

## Settings

Tap **⚙** on any card to change these settings. They're applied in layers:

1. **Built-in defaults**, in `src/cardRef.ts`.
2. **The template's `KR_CONFIG` block**, in both the Front and Back templates. It syncs, so it sets the defaults for every device.
3. **Per-device changes made in ⚙.** They're stored in the webview's `localStorage`, with a cookie as fallback. A blue dot marks each changed field, and ↺ undoes the change.

How to keep a change made in ⚙:

- **Anki desktop + the [add-on](../addon/README.md):** **Save for all devices** rewrites the `KR_CONFIG` block in this note type's templates, so the change syncs everywhere. Without the add-on, desktop changes last only until Anki restarts, because Anki's card webview doesn't keep storage.
- **Anywhere:** **Copy as template config** gives you a block to paste over `KR_CONFIG` in both templates.

Invalid values, unknown keys and a `KR_CONFIG` script that fails to run show a ⚠ banner on the card. The template sets `KR_CONFIG = null` before its config script, so a syntax error is detected instead of silently ignored.

| Setting | Default | Options |
|---|---|---|
| `intro` | `'lastMove'` | `'lastMove'` animates the opponent's last move. `'fullGame'` replays the game at `introMoveDelay` ms per move; tap the board to skip. |
| `afterAnswer` | `'flip'` | `'flip'` shows the back after `flipDelay` ms. `'stay'` stays on the front. `'grade'` flips, then answers automatically after `gradeDelay` ms with `gradeSolved` (3 Good), `gradeHinted` (2 Hard) or `gradeFailed` (1 Again). Any tap or key cancels the automatic answer. |
| `hints` | `true` | Show the Hint button. |
| `acceptNags` | `[1, 3, 5, 8]` | NAGs counted as correct. |
| `strictMate` | `false` | Forced mates: a move must keep a forced mate whatever its NAG (when a candidate's `[%eval #N]` shows one for the side to move); with `true`, only the fastest mate counts. Live play too. |
| `autoReplyDelay` | `400` | ms before the opponent's reply. |
| `showEval` / `evalOnPuzzleFront` | `true` / `false` | Eval bar and numbers; whether to show them at the puzzle position on the front. |
| `live` | `false` | Keep playing past the PGN with a live engine. |
| `acceptNeutral` | `false` | Also accept moves with no NAG (normal play: losing a little more than a good move, under an inaccuracy). |
| `goodMove` | `1` | Live play: win-% a move may lose against the engine's best and still count. |
| `liveMaxMoves` | `10` | Live play: at most this many of your moves. |
| `timed` | `false` | Timed puzzles: a clock runs on the front while you think (not during the opponent's reply or the engine's), and the back shows the time taken. |
| `timeLimit` | `30` | Timed: seconds for the whole puzzle (5–600). |
| `timeAffects` | `'nothing'` | Timed: when time is up, `'nothing'` only shows the time, `'fail'` ends the attempt as failed and shows the answer, `'grade'` lets you finish but a solve over the limit is graded `gradeSlow` (with `afterAnswer: 'grade'`). |
| `gradeSlow` | `2` | Ease for a slow solve (1 Again … 4 Easy). It never grades better than the hint/solved ease. |
| `showClock` | `true` | Show the clock on the front; off shows the time only on the back. |

The card never flips or grades by itself, it asks the host: `pycmd` on desktop, AnkiDroid's own global
`showAnswer()` / `buttonAnswerEaseN()` on AnkiDroid (no AnkiDroid JS API needed). AnkiDroid's new study
screen (2.25+) flips the same way but has nothing to grade with, so there `afterAnswer: 'grade'` only
shows a note and you grade with its own buttons. AnkiMobile and AnkiWeb have no hook at all, so the card
stays on the front there and asks you to show the answer.

## Anki notes

- The templates load `_knightly.js` with a script tag injected at runtime, and remount on every card,
  because Desktop and AnkiDroid reuse the webview.
- The front's result reaches the back through `window` and `sessionStorage`. When neither is available,
  the back still works but shows no "you played…" banner.

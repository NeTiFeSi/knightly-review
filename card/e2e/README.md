# End-to-end tests in real Anki

`run.mjs` drives the card in **real Anki desktop** (over AnkiConnect + Chrome DevTools) and in **AnkiDroid on a phone** (over adb on Wi-Fi + WebView DevTools). It runs the same scenarios on both. It works only with a separate test install, never your own collection:

- **Desktop:** the profile `Knightly Test` in its own base folder, with AnkiConnect on port 8766.
- **Phone:** a separate AnkiDroid install: the "parallel" build `com.ichi2.anki.a`, or a clone ("Dual apps", Android user 999) with `KR_DROID_USER=999 KR_DROID_PACKAGE=com.ichi2.anki`. Both AnkiDroid study screens (old reviewer, new study screen) are driven.

```sh
node e2e/run.mjs list                                  # scenarios
node e2e/run.mjs start                                 # start the desktop test Anki (your Anki must be closed)
node e2e/run.mjs deploy --target desktop               # push dist/ into the test profile (keeps its KR_CONFIG)
node e2e/run.mjs --target desktop                      # run everything on desktop
node e2e/run.mjs --target android --device IP:PORT     # … on the phone (IP:PORT from Wireless debugging)
node e2e/run.mjs --target both --only solve,hint       # pick scenarios
node e2e/run.mjs status --target both                  # is everything reachable?
node e2e/run.mjs stop
node e2e/run.mjs deploy --target user                  # dry run of updating YOUR Anki; add --yes to apply
```

Screenshots go to `e2e/out/<target>/`. The exit code is 1 when a scenario fails. The answers for each card are computed with the card's own PGN and grading code (`oracle.ts`), so the scenarios work on any card in the sample deck.

## One-time setup

**Desktop**
1. Create a base folder (e.g. `~/.local/share/Anki2-knightly-test`) with one profile, `Knightly Test`. Don't log in to AnkiWeb in it.
2. In its `addons21/`:
   - copy AnkiConnect and set `webBindPort` to **8766**;
   - symlink `../addon/knightly_review`.
3. Import `dist/knightly-sample.apkg` (`npm run apkg`).

**Phone** (Android 11+)
1. Install AnkiDroid's parallel build, `AnkiDroid-<version>.parallel.A.apk` from its GitHub releases, with `adb install`. Don't log in to AnkiWeb in it.
2. In the test app, turn on Developer options (Settings → About → tap the logo 6 times), then **HTML / JavaScript debugging**. Import `knightly-sample.apkg`.
3. On the phone, turn on **Wireless debugging**, then pair once with `adb pair IP:PORT CODE`.
4. On Xiaomi phones, also turn on **USB debugging (Security settings)** and restart the phone, so taps work.
5. Optional, a fixed port: after connecting once with the Wireless-debugging port, run `adb -s IP:PORT tcpip 5555`. Then use `--device IP:5555` until the phone restarts. Reserving the phone's IP in the router keeps the address stable too.

Settings, through environment variables:
- `KR_ANKI_BASE`, `KR_ANKI_PROFILE`, `KR_ANKI_PORT` (8766), `KR_DEVTOOLS_PORT` (9222);
- `KR_ADB_SERIAL`, `KR_DROID_PACKAGE` (`com.ichi2.anki.a`), `KR_DROID_USER` (empty; 999 for a clone), `KR_DROID_MEDIA`, `KR_DROID_DEVTOOLS_PORT` (9223).

## Anki quirks found this way

- **Desktop:** the reviewer renders the back from the output cached when the front was shown. The add-on re-renders the card after saving settings.
- **Desktop:** the card webview uses an off-the-record profile, so `localStorage` doesn't survive a restart.
- **AnkiDroid:** each study screen is served from a new random port, so `localStorage` is lost. The ⚙ settings survive through the cookie fallback.
- **AnkiDroid:** the WebView writes cookies to disk only when the app goes to the background (or about every 30 s).
- **AnkiDroid:** the review intent must be started with `--activity-clear-task`, or Android just brings the existing task forward.
- **AnkiDroid 2.25 has two study screens.** The old reviewer's globals `showAnswer()` / `buttonAnswerEaseN()` navigate to `signal:` URLs. The new study screen (`previewer.CardViewerActivity`, Settings → New study screen) runs Anki's own reviewer scripts (`window.ankiPlatform === 'ankidroid'`): `showAnswer()` works there, but any `signal:` URL (so `buttonAnswerEaseN()` and the `AnkiDroidJS` API) only raises its "features not yet supported" dialog and does nothing. The runner therefore never uses the JS API; it grades with the native button ids (`com.ichi2.anki:id/again_button`) when a scenario needs the next card.
- **AnkiDroid new study screen:** its activity is not exported, so `am start` can't bring it forward; `launch()` moves its task to the front with `am task lock` / `lock stop`. It keeps neither cookies nor localStorage over an app restart (the `settings-survive` scenario is skipped there).
- **AnkiDroid's "Enable AnkiDroid API"** (the ContentProvider) can't be used from `adb shell` (`Permission not granted for: CardContentProvider.query`).
- **AnkiDroid:** AnkiDroid's `reviewer.css` gives bare `<button>`s a margin; the card resets it (`.kr-root :where(button, …)`) so the gear and controls stay aligned.

## README pictures

`node e2e/screenshots.mjs [--anki]` (after `npm run build`) rewrites `docs/img/` with the sample games and some arrows drawn by hand.
- **Without `--anki`:** the card pictures come from the dev harness in headless Chromium (`KR_CHROME` picks the binary). No Anki is needed. Only this mode makes the phone-sized picture, which is a browser window, not AnkiDroid.
- **With `--anki`** (the test Anki running, `deploy` done): pictures and the short GIFs (8 fps, via ffmpeg) come from the real reviewer, live play included, and the add-on's Qt windows (settings, Stockfish, update progress) are saved through the test-only helper add-on `e2e/helper/kr_shots`. It serves `127.0.0.1:8767`, shows each window with sample data and saves `widget.grab()` of it. Install it once, in the test profile only: `ln -s "$PWD/e2e/helper/kr_shots" ~/.local/share/Anki2-knightly-test/addons21/kr_shots`, then restart the test Anki.

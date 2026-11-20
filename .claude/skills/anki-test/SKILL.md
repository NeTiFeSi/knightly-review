---
name: anki-test
description: Run Knightly Review card UI tests in real Anki desktop and/or AnkiDroid (on a phone over Wi-Fi adb), using the isolated test profile and test app. Use after changing card/src, card/templates or addon/, or when asked to test the card UI "in Anki", "on the phone" or "on AnkiDroid".
---

# Test the card UI in real Anki

The runner is `card/e2e/run.mjs` (`npm run e2e --` from `card/`). It drives:
- **desktop**: an isolated Anki through AnkiConnect (port **8766**) and Chrome DevTools (9222);
- **android**: the AnkiDroid test app `com.ichi2.anki.a` on a phone, through adb over Wi-Fi and WebView DevTools (forwarded to 9223).

Scenarios are listed by `node e2e/run.mjs list`. Each one checks the card the way a person would use it: solving on the board, failing, hints, flip, auto-grade and ⚙ settings.

## Safety rules (never break these)

- **Never test in the user's own Anki or AnkiDroid.** Use only:
  - the base folder `~/.local/share/Anki2-knightly-test` (profile `Knightly Test`, AnkiConnect 8766);
  - the phone app `com.ichi2.anki.a`.

  The tools refuse anything else; don't work around them.
- **Never use port 8765.** That's the user's own AnkiConnect. Never call `sync` on any profile.
- Only one Anki desktop can run at a time. `start` refuses while the user's Anki is open. Pass `--close-user-anki` only when the user has said Anki may be closed or restarted (they have, for this project), and tell them you did it.
- On the phone, act only on the `Knightly Review (sample)` deck. The runner opens that deck by its id (saved in `card/e2e/.state.json`, first found by name in the test app's collection, copied over adb) and only drives a page that has the Knightly card. It needs no AnkiDroid JS API.
- `auto-grade` answers **Again**, so the sample cards stay in learning and the deck never runs out. Don't change that to real grades.

## Steps

All commands run from `card/`.

1. **Build:** `npm run build`. Also run `npm test` and `npm run typecheck` if you changed code.
2. **Desktop:**
   - If you changed `addon/` or `core/`: the test base has a **copy** of the add-on, not a symlink (a symlink would share `meta.json` and `user_files/` with the user's own Anki). Refresh it with `python3 build.py --dev` and the `rsync` line in `addon/README.md` (it keeps the test config), then restart the test Anki.
   - `node e2e/run.mjs start` (add `--close-user-anki` if allowed and needed);
   - `node e2e/run.mjs deploy --target desktop` pushes `dist/` into the test profile and keeps its `KR_CONFIG`;
   - `node e2e/run.mjs --target desktop`.
3. **Android:**
   - `node e2e/run.mjs status --target android --device <phone-ip>:5555`. The phone usually listens on the **fixed port 5555** (`adb tcpip 5555`), which lasts until the phone restarts.
   - If it says no phone is connected (the phone restarted, so port 5555 is gone): ask the user to turn on Wireless debugging and send the **IP & port** from that screen (pairing is kept). Then run `adb connect <ip:port>` and `adb -s <ip:port> tcpip 5555`, and use `<ip>:5555` again.
   - Pass `--device` to every command, or set `KR_ADB_SERIAL`.
   - `node e2e/run.mjs deploy --target android` pushes `_knightly.js` into the test app's media.
   - **Template / CSS changes:** `npm run apkg && node e2e/run.mjs import --target android`. It pushes the apkg to the test app's own storage, opens it through a `content://` URI, presses Add and Import, and restarts the app (the study screen caches the old template otherwise). Don't ask the user to import by hand.
   - **Test app as a clone ("Dual apps"):** if the test app is the Xiaomi clone of AnkiDroid (same package `com.ichi2.anki`, Android user 999), set `KR_DROID_USER=999 KR_DROID_PACKAGE=com.ichi2.anki` (plus `KR_ADB_SERIAL`). The runner tells it apart from the real app by its non-zero user id, uses `/storage/emulated/999`, and refuses user 0. Check with `adb shell pm list users`.
   - **AnkiDroid 2.25+ has two study screens, and the runner drives both** (`t.studyScreen()` is `old` for activity `Reviewer`, `new` for `previewer.CardViewerActivity`). Switch in the test app: drawer → Settings → "New study screen" → first switch. Run the suite on both before calling AnkiDroid work done. On the new one the card can flip but not grade, so `auto-grade` expects the note and presses the native Again button, and `settings-survive` is skipped (that screen keeps no cookie or localStorage over a restart). Don't enable "Enable AnkiDroid API": `adb shell` can't use it anyway.
   - `node e2e/run.mjs --target android --device <ip:port>`.
4. **Pick scenarios** with `--only name1,name2`.
5. **Report** the PASS/FAIL table, and look at the screenshots in `card/e2e/out/<target>/` (`<scenario>-<step>.png`, plus `-failure.png`) before calling something fixed. A FAIL is a wrong result; an ERROR is a broken step (timeout, missing element). Read the note in either case.
6. **When done:** `node e2e/run.mjs stop` if you started the desktop test Anki. Tell the user their own Anki can be reopened.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `active profile is "…"` | The wrong Anki is running. Stop it and run `start`. |
| `your own Anki is running` | Ask the user, or use `--close-user-anki` if they allowed it. |
| `no phone connected` / `Connection refused` | Wireless debugging is off or got a new port: ask the user for the IP & port. |
| `the test app's WebView` timeout | HTML / JavaScript debugging is off in the test app (Settings → Developer options), or the app wasn't on the study screen. |
| `a Knightly card in the study screen` timeout | The study screen shows another deck, or the saved deck ID is stale. Delete `card/e2e/.state.json` and rerun, so the deck is found by name again. |
| Phone stuck on the deck list | The review intent was "delivered to top". The runner already starts it with `--activity-clear-task`; if you call `am start` yourself, do the same. |
| `protocol fault` from adb | The phone is reachable twice (port 5555 and the paired port). The runner retries once; `adb disconnect` the extra entry if it persists. |
| `input tap` / `INJECT_EVENTS` errors | On Xiaomi phones, "USB debugging (Security settings)" must be on, and the phone restarted afterwards. |
| The desktop back side has old settings | Anki caches the rendered card. The add-on re-renders after "Save for all devices"; reopen the deck for other template changes. |

On AnkiDroid, the WebView writes cookies to disk only when the app goes to the background (or about every 30 s). Killing the app straight from the study screen can lose a just-changed ⚙ setting, which no real user can do. `restartApp()` leaves the app first, like a person would.

Desktop `localStorage` doesn't survive an Anki restart (off-the-record profile). AnkiDroid serves each study screen from a new port, so ⚙ settings survive there through the cookie fallback.

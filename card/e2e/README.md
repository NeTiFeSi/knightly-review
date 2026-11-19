# End-to-end tests in real Anki

These scripts drive the real Anki desktop reviewer over AnkiConnect and Chrome DevTools. Run them only against a **separate test install**, never your own collection: `anki.mjs` refuses to run unless the active profile is `Knightly Test`.

## One-time setup (desktop)

1. Create a separate base folder with one profile, `Knightly Test`. Don't log in to AnkiWeb in it.
2. In its `addons21/`:
   - Copy AnkiConnect and set `webBindPort` to **8766**, so it can't be confused with your own Anki (8765).
   - Symlink `addon/knightly_review`.
3. Import `dist/knightly-sample.apkg` (`npm run apkg`).

## Run

```sh
# Only one Anki can run at a time: close your own first.
QTWEBENGINE_REMOTE_DEBUGGING=127.0.0.1:9222 anki -b ~/.local/share/Anki2-knightly-test -p "Knightly Test" &
npm run build
npm run anki:deploy   # push dist/ into the test profile (keeps its KR_CONFIG)
npm run anki:smoke    # open the sample deck; check the card mounts and the add-on answers
```

Settings: `KR_ANKI_PORT` (8766), `KR_DEVTOOLS_PORT` (9222), `KR_ANKI_PROFILE` (`Knightly Test`).

## Anki quirks found this way

- The reviewer renders the back from the output cached when the front was shown. The add-on re-renders the card after saving settings.
- Anki desktop's card webview uses an off-the-record profile, so `localStorage` doesn't survive a restart.

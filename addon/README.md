# Knightly Review add-on (Anki desktop, optional)

This add-on lets the card's ⚙ settings dialog save settings into the note type's templates, so they sync to AnkiDroid, AnkiMobile and AnkiWeb. Without it, the cards work normally, but settings changed on desktop are forgotten when Anki restarts. Anki's card webview doesn't keep browser storage.

It listens for two messages from the card:

- `pycmd('kr:hello')` → `{kr: 1}`
- `pycmd('kr:config:{…}')`

A config message is validated, then replaces the `window.KR_CONFIG = {…};` block in every template of the current card's note type. It only answers in the reviewer and the previewer.

```sh
python3 build.py                       # dist/knightly_review.ankiaddon → double-click to install
python3 -m unittest discover -s tests  # tests for the template rewrite (no Anki needed)
```

For development, symlink `knightly_review/` into Anki's `addons21` folder and restart Anki.

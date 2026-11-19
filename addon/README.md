# Knightly Review add-on (Anki desktop)

The add-on does three jobs:

1. **Makes cards from your games** (Tools → Knightly Review). It runs the core (`../core/knightly`) in a background thread, then writes the notes into the collection as one undoable step.
2. **Plays on with your engines during review.** A card asks `pycmd('kr:engine:{id, fen, multipv, moves}')`; the answer arrives later through `window.__krEngine.resolve(id, …)`. `kr:hello` reports which engine is available.
3. **Saves card settings for all devices.** The card's ⚙ dialog can write its settings into the note type's templates, so they sync to AnkiDroid, AnkiMobile and AnkiWeb. The card talks to it with `pycmd('kr:hello')` → `{kr: 1}` and `pycmd('kr:config:{…}')`. A config message is validated, then replaces the `window.KR_CONFIG = {…};` block in every template of the current card's note type. It only answers in the reviewer and the previewer.

| File | Does |
|---|---|
| `__init__.py` | The `pycmd` handler; sets up the menu and hooks |
| `runner.py` | Background updates: thread, cancel on profile close, startup and periodic scheduling |
| `live.py` | Live engine for cards (`pycmd('kr:engine:{…}')`): own engine processes, answers via `web.eval`; pauses background analysis for 30 s after each request |
| `ui.py` | Settings (also the first-run welcome), engine finder/installer, progress window |
| `collection.py` | Note type, `_knightly.js` media, adding/updating notes (pylib only, no Qt) |
| `settings.py` | Add-on config, `user_files/` paths, the Lichess token file |
| `config_block.py` | Validating and rewriting the `KR_CONFIG` block |

Local data lives in `knightly_review/user_files/` (cache, token, downloaded engines), which Anki keeps across add-on updates.

## Build

```sh
(cd ../card && npm run build)          # the card bundle and templates
python3 build.py                       # dist/knightly_review.ankiaddon → double-click to install
python3 build.py --dev                 # only refresh knightly_review/vendor and knightly_review/card
python3 -m unittest discover -s tests  # needs Anki's `anki` package for the collection tests
```

`build.py` bundles the core and the packages pinned in `vendor.txt` into `knightly_review/vendor/`. It rewrites their imports to be relative (`import chess` → `from .. import chess`), because AnkiWeb installs add-ons under a numeric folder name and all add-ons share one `sys.modules`: another add-on's `chess` must never be picked up, and ours must never leak. Only pure-Python packages go in `vendor.txt`. Updating one means bumping its version and sdist sha256 there, rebuilding, and running all tests.

## Developing against a real Anki

`knightly_review/meta.json` (the config) and `knightly_review/user_files/` live inside the add-on folder. If two Anki base folders symlink the same source folder, they share settings and data. So the test base gets a **copy**, refreshed after each change:

```sh
python3 build.py --dev
rsync -a --delete --exclude meta.json --exclude 'user_files/*' --exclude __pycache__ \
  knightly_review/ ~/.local/share/Anki2-knightly-test/addons21/knightly_review/
```

Then restart the test Anki (`node e2e/run.mjs stop` / `start` in `card/`).

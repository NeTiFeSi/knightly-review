# Contributing

Thanks for helping. Knightly Review is licensed under the AGPL-3.0-or-later; by contributing you agree that your
changes are released under it.

## Layout

```
core/    Python package `knightly` (no Anki imports): games, analysis, puzzles, notes
addon/   the Anki add-on; bundles core/ and python-chess
card/    the interactive card (TypeScript), built into one media file
```

Each folder has a README with its details: [core](core/README.md), [addon](addon/README.md),
[card](card/README.md).

## Set up

You need Python 3.11 or newer and Node.js 24 or newer.

```sh
python -m venv .venv && . .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e core --group core/pyproject.toml:dev   # the core, plus ruff
pip install anki                                      # only for the add-on's collection tests
(cd card && npm ci)
```

## Check your work

```sh
ruff check . && ruff format --check .                 # style; rules are ruff's defaults, nothing is ignored
(cd core && python -m unittest discover -s tests)     # no network or engine needed
(cd card && npm run typecheck && npm test)
(cd card && npm run build)
python addon/build.py --dev                           # bundles core and python-chess into the add-on folder
(cd addon && python -m unittest discover -s tests)
```

`ruff format .` fixes formatting (120 columns). CI runs all of this on Linux, Windows and macOS
(`.github/workflows/ci.yml`).

Changes to the card or the add-on's windows also need a try in real Anki: `card/e2e/README.md` describes an isolated
test profile and an AnkiDroid runner. **Never test in your own Anki profile.**

## Guidelines

- Write plain, typed Python for 3.11+ and fix lint findings instead of silencing them; there are no `noqa` comments.
- The add-on bundles only pure-Python packages (`addon/vendor.txt`, pinned with a sha256). Don't add one with compiled
  parts.
- `core/knightly/cook.py` and `cookutil.py` are ported from lichess-puzzler; keep their logic in step with upstream
  (see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
- Add a line to `CHANGELOG.md` under "Unreleased" for anything a user would notice.

## Releasing (maintainers)

1. Choose the version. `knightly.__version__` in `core/knightly/__init__.py` is the single source of truth. Set it,
   then run `cd card && npm version <version> --no-git-tag-version` so `package.json` and its lockfile follow. A test
   fails if they differ.
2. In `CHANGELOG.md`, rename "Unreleased" to `[<version>] - <date>`, add a new empty "Unreleased", and update the
   compare links at the bottom.
3. Merge to `master`, then tag and push: `git tag -a v<version> -m "v<version>" && git push origin v<version>`.
   A tag such as `v0.2.0-rc1` publishes a pre-release.
4. `.github/workflows/release.yml` re-runs CI, checks the tag against `__version__`, builds `knightly_review.ankiaddon`
   and `knightly-sample.apkg`, and creates the GitHub release with the changelog section as its notes.
5. Upload the `.ankiaddon` to [AnkiWeb](https://ankiweb.net/shared/addons) with the text in
   `docs/ankiweb-listing.md`.

---
name: anki-deploy
description: Update the user's OWN Anki desktop with a new Knightly Review build (the _knightly.js bundle, templates and styling) while keeping their KR_CONFIG settings. Use when the user asks to send, push or install the card changes into their Anki, as opposed to the test profile.
---

# Deploy a build to the user's own Anki

This writes to the user's real collection, so every step is explicit and confirmed. For testing, use the `anki-test` skill and the test profile instead.

## Rules

- **Never overwrite the user's settings.** The deploy keeps the `window.KR_CONFIG = {…}` block that each template already has. Only the rest of the template, the bundle and the styling change.
- **Dry run first, then ask.** Show the user the list of changes, and get a yes before applying.
- **Don't sync on the user's behalf.** They sync to reach AnkiDroid and other devices.
- Never use the test profile's port (8766) for this, nor the user's port (8765) for tests.

## Steps

All commands run from `card/`.

1. `npm run build`, plus `npm test` and `npm run typecheck`. Ideally, also pass `anki-test` on desktop first.
2. Make sure the user's Anki is running, with AnkiConnect on 8765. If the test Anki is running instead, stop it (`node e2e/run.mjs stop`) and ask the user to open theirs.
3. **Dry run:** `node e2e/run.mjs deploy --target user`. It lists what would change: the bundle hash, which templates (always "your KR_CONFIG kept"), and the styling. If a template has no `KR_CONFIG` block, it says so; mention that to the user.
4. Show the list to the user and ask to apply.
5. **Apply:** `node e2e/run.mjs deploy --target user --yes`.
6. Tell the user:
   - restart Anki desktop, because the reviewer caches `_knightly.js`;
   - sync, then restart AnkiDroid, so the phone gets the new bundle.

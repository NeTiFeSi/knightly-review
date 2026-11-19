// Push dist/ (bundle, templates, styling) into an Anki profile through AnkiConnect,
// keeping the KR_CONFIG block that profile already has.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { CARD_DIR, MODEL } from './common.mjs';
import { ankiConnect } from './desktop.mjs';

const BLOCK = /window\.KR_CONFIG\s*=\s*\{[^{}]*\}\s*;?/;
const hash = s => createHash('sha256').update(s).digest('hex').slice(0, 10);

function built() {
  const read = f => readFileSync(CARD_DIR + 'dist/' + f, 'utf8');
  return {
    bundle: readFileSync(CARD_DIR + 'dist/_knightly.js'),
    front: read('templates/front.html'),
    back: read('templates/back.html'),
    css: read('templates/style.css'),
  };
}

/**
 * Plan (and with `apply`, perform) a deploy to the Anki on `port`.
 * Returns a list of human-readable changes.
 */
export async function deploy(port, { apply = false } = {}) {
  const ac = (a, p) => ankiConnect(port, a, p);
  const b = built();
  const changes = [];

  const media = await ac('retrieveMediaFile', { filename: '_knightly.js' });
  const oldBundle = media ? Buffer.from(media, 'base64') : null;
  if (!oldBundle || !oldBundle.equals(b.bundle))
    changes.push(`_knightly.js: ${oldBundle ? hash(oldBundle) + ' → ' : 'new, '}${hash(b.bundle)} (${b.bundle.length} bytes)`);

  const models = await ac('modelNames');
  if (!models.includes(MODEL)) throw new Error(`note type "${MODEL}" not found; import the sample .apkg first`);
  const cur = await ac('modelTemplates', { modelName: MODEL });
  const templates = {};
  for (const [name, t] of Object.entries(cur)) {
    templates[name] = {};
    for (const [side, fresh] of [
      ['Front', b.front],
      ['Back', b.back],
    ]) {
      const kept = (t[side].match(BLOCK) || [])[0];
      const next = kept ? fresh.replace(BLOCK, () => kept) : fresh;
      templates[name][side] = next;
      if (next !== t[side])
        changes.push(`template ${name}/${side}: updated${kept ? ' (your KR_CONFIG kept)' : ' (no KR_CONFIG found: default added)'}`);
    }
  }
  const css = await ac('modelStyling', { modelName: MODEL });
  if (css.css !== b.css) changes.push('styling: updated');

  if (apply && changes.length) {
    if (!oldBundle || !oldBundle.equals(b.bundle))
      await ac('storeMediaFile', { filename: '_knightly.js', data: b.bundle.toString('base64') });
    await ac('updateModelTemplates', { model: { name: MODEL, templates } });
    if (css.css !== b.css) await ac('updateModelStyling', { model: { name: MODEL, css: b.css } });
  }
  return changes;
}

// Push dist/ (bundle, templates, styling) into an Anki profile through AnkiConnect,
// keeping the KR_CONFIG block that profile already has.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { CARD_DIR, MODEL } from './common.mjs';
import { ankiConnect } from './desktop.mjs';

const BLOCK = /window\.KR_CONFIG\s*=\s*\{[^{}]*\}\s*;?/;
export const MEDIA = ['_knightly.js'];
const hash = s => createHash('sha256').update(s).digest('hex').slice(0, 10);

function built() {
  const read = f => readFileSync(CARD_DIR + 'dist/' + f, 'utf8');
  return {
    // The card bundle.
    media: Object.fromEntries(MEDIA.map(f => [f, readFileSync(CARD_DIR + 'dist/' + f)])),
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

  const mediaUpdates = [];
  for (const [filename, data] of Object.entries(b.media)) {
    const cur = await ac('retrieveMediaFile', { filename });
    const old = cur ? Buffer.from(cur, 'base64') : null;
    if (old && old.equals(data)) continue;
    mediaUpdates.push([filename, data]);
    changes.push(`${filename}: ${old ? hash(old) + ' → ' : 'new, '}${hash(data)} (${data.length} bytes)`);
  }

  const models = await ac('modelNames');
  if (!models.includes(MODEL)) throw new Error(`note type "${MODEL}" not found; import the sample .apkg first`);
  const fields = await ac('modelFieldNames', { modelName: MODEL });
  const needVisits = !fields.includes('Visits'); // the templates read it (which replies each card showed)
  if (needVisits) changes.push('field Visits: added');
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
    if (needVisits) await ac('modelFieldAdd', { modelName: MODEL, fieldName: 'Visits' });
    for (const [filename, data] of mediaUpdates)
      await ac('storeMediaFile', { filename, data: data.toString('base64'), deleteExisting: true });
    await ac('updateModelTemplates', { model: { name: MODEL, templates } });
    if (css.css !== b.css) await ac('updateModelStyling', { model: { name: MODEL, css: b.css } });
  }
  return changes;
}

// Push the current build (dist/) into the TEST profile: media file, templates, styling.
// Keeps the KR_CONFIG block that is already in the profile's templates.
import { readFileSync } from 'node:fs';
import { ac, assertTestProfile } from './anki.mjs';

await assertTestProfile();
const dist = new URL('../dist/', import.meta.url);
const read = f => readFileSync(new URL(f, dist), 'utf8');
const BLOCK = /window\.KR_CONFIG\s*=\s*\{[^{}]*\}\s*;?/;
await ac('storeMediaFile', { filename: '_knightly.js', data: readFileSync(new URL('_knightly.js', dist)).toString('base64') });
const cur = await ac('modelTemplates', { modelName: 'Knightly Review' });
const templates = {};
for (const [name, t] of Object.entries(cur)) {
  templates[name] = {};
  for (const [side, file] of [['Front', 'templates/front.html'], ['Back', 'templates/back.html']]) {
    const kept = (t[side].match(BLOCK) || [])[0];
    const fresh = read(file);
    templates[name][side] = kept ? fresh.replace(BLOCK, () => kept) : fresh;
  }
}
await ac('updateModelTemplates', { model: { name: 'Knightly Review', templates } });
await ac('updateModelStyling', { model: { name: 'Knightly Review', css: read('templates/style.css') } });
console.log('deployed to test profile; kept KR_CONFIG:', Object.values(cur).every(t => BLOCK.test(t.Front)));

// Open the sample deck and check that the card mounts and the add-on answers.
import { ac, assertTestProfile, connect, reviewer, wait } from './anki.mjs';

await assertTestProfile();
await ac('guiDeckReview', { name: 'Knightly Review (sample)' });
await wait(1500);
const browser = await connect();
const page = await reviewer(browser);
const errors = [];
page.on('pageerror', e => errors.push(e.message));
await page.evaluate(() => window.KnightlyReview && window.KnightlyReview.mount());
await wait(800);
console.log('card:', await page.evaluate(() => {
  const r = document.getElementById('kr-root');
  return r ? r.dataset.side + ' | ' + ((r.querySelector('.kr-status') || {}).innerText || '').replace(/\n/g, ' | ') : 'no #kr-root';
}));
console.log('add-on:', await page.evaluate(() => new Promise(res => { pycmd('kr:hello', res); setTimeout(() => res('no answer'), 1500); })));
if (process.argv[2]) await page.screenshot({ path: process.argv[2] });
console.log('errors:', errors.length ? errors : 'none');
browser.disconnect();

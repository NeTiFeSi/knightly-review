// Helpers for driving a real Anki desktop over AnkiConnect + Chrome DevTools.
// Use an isolated test install, never your own collection:
//   QTWEBENGINE_REMOTE_DEBUGGING=127.0.0.1:9222 anki -b ~/.local/share/Anki2-knightly-test -p "Knightly Test"
// with AnkiConnect configured on KR_ANKI_PORT (default 8766). See e2e/README.md.
import puppeteer from 'puppeteer-core';

export const ANKI = 'http://127.0.0.1:' + (process.env.KR_ANKI_PORT || 8766);
export const DEVTOOLS = 'http://127.0.0.1:' + (process.env.KR_DEVTOOLS_PORT || 9222);
export const PROFILE = process.env.KR_ANKI_PROFILE || 'Knightly Test';
export const wait = ms => new Promise(r => setTimeout(r, ms));

export async function ac(action, params = {}) {
  const r = await fetch(ANKI, { method: 'POST', body: JSON.stringify({ action, version: 6, params }) });
  const j = await r.json();
  if (j.error) throw new Error(action + ': ' + j.error);
  return j.result;
}

/** Refuse to run against anything but the test profile. */
export async function assertTestProfile() {
  const p = await ac('getActiveProfile');
  if (p !== PROFILE) throw new Error(`Active Anki profile is "${p}", expected "${PROFILE}". Refusing to run.`);
}

export function connect() {
  return puppeteer.connect({ browserURL: DEVTOOLS, defaultViewport: null });
}

/** The main webview (the reviewer while reviewing). */
export async function reviewer(browser) {
  const pages = await browser.pages();
  for (const p of pages) if ((await p.title().catch(() => '')) === 'main webview') return p;
  for (const p of pages) if (await p.$('#qa').catch(() => null)) return p;
  throw new Error('no reviewer page: ' + (await Promise.all(pages.map(p => p.title()))).join(', '));
}

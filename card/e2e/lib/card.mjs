// Actions and reads on a mounted Knightly Review card (a puppeteer Page).
// Shared by every target; `pointer` is 'mouse' (desktop) or 'touch' (AnkiDroid).
import { until, wait } from './common.mjs';

const SETTINGS_KEY = 'kr:settings:v1';

/** What the card shows right now. */
export function read(page) {
  return page.evaluate(() => {
    const root = document.getElementById('kr-root');
    const cloze = document.querySelector('#kr-cloze .cloze');
    const status = document.querySelector('.kr-status');
    return {
      mounted: !!(root && root.querySelector('.kr-main')),
      error: (document.querySelector('.kr-error') || {}).textContent || '',
      side: root ? root.dataset.side : '',
      n: cloze ? Number(cloze.getAttribute('data-ordinal') || (cloze.textContent || '').replace(/\D/g, '')) : 0,
      status: status ? status.innerText.replace(/\n+/g, ' | ') : '',
      warning: (document.querySelector('.kr-warn-banner') || {}).textContent || '',
      panel: !!document.querySelector('.kr-settings'),
      orientation: (document.querySelector('.cg-wrap') || { classList: { contains: () => false } }).classList.contains(
        'orientation-black',
      )
        ? 'black'
        : 'white',
    };
  });
}

/** The PGN field as plain text, decoded the same way the card does. */
export function pgnText(page) {
  return page.evaluate(() => {
    const el = document.getElementById('kr-pgn');
    const html = el ? el.innerHTML : '';
    const ta = document.createElement('textarea');
    ta.innerHTML = html.replace(/<br\s*\/?>/gi, '\n').replace(/<\/(div|p)>/gi, '\n').replace(/<[^>]*>/g, '');
    return ta.value.replace(/ /g, ' ');
  });
}

export function waitMounted(page, side, timeout = 15000) {
  return until(
    async () => {
      const s = await read(page);
      if (s.error) throw new Error('card error: ' + s.error);
      return s.mounted && (!side || s.side === side) ? s : null;
    },
    'the card' + (side ? ' (' + side + ')' : ''),
    timeout,
  );
}

export function waitStatus(page, re, timeout = 8000) {
  return until(async () => re.test((await read(page)).status) && read(page), 'status ' + re, timeout);
}

/** Replace this device's settings (both storages) and remount the card. */
export async function setOverrides(page, overrides) {
  await page.evaluate(
    (key, json) => {
      try {
        if (json) localStorage.setItem(key, json);
        else localStorage.removeItem(key);
      } catch (_) {}
      document.cookie = json
        ? 'kr_settings=' + encodeURIComponent(json) + '; max-age=315360000; path=/; SameSite=Lax'
        : 'kr_settings=; max-age=0; path=/; SameSite=Lax';
      window.KnightlyReview.mount();
    },
    SETTINGS_KEY,
    overrides && Object.keys(overrides).length ? JSON.stringify(overrides) : '',
  );
  await wait(300);
}

export const clearOverrides = page => setOverrides(page, null);

export function storedOverrides(page) {
  return page.evaluate(key => {
    let ls = null;
    try {
      ls = localStorage.getItem(key);
    } catch (_) {}
    const m = /(?:^|;\s*)kr_settings=([^;]*)/.exec(document.cookie);
    return { localStorage: ls, cookie: m ? decodeURIComponent(m[1]) : null };
  }, SETTINGS_KEY);
}

export function click(page, selector) {
  return page.evaluate(sel => {
    const el = document.querySelector(sel);
    if (!el) throw new Error('no element ' + sel);
    el.click();
  }, selector);
}

export function clickText(page, selector, text) {
  return page.evaluate(
    (sel, t) => {
      const el = [...document.querySelectorAll(sel)].find(e => e.textContent.trim() === t);
      if (!el) throw new Error('no ' + sel + ' "' + t + '"');
      el.click();
    },
    selector,
    text,
  );
}

/** Tap or click the centre of `square` (e.g. 'e5') on the board. */
export async function pressSquare(page, square, pointer) {
  const board = await page.$('cg-board');
  if (!board) throw new Error('no board');
  const box = await board.boundingBox();
  const { orientation } = await read(page);
  const s = box.width / 8;
  const f = square.charCodeAt(0) - 97;
  const r = Number(square[1]) - 1;
  const col = orientation === 'white' ? f : 7 - f;
  const row = orientation === 'white' ? 7 - r : r;
  const x = box.x + (col + 0.5) * s;
  const y = box.y + (row + 0.5) * s;
  if (pointer === 'touch') await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);
  await wait(200);
}

/** Play a UCI move by pressing the two squares (promotion picks the queen). */
export async function play(page, uci, pointer) {
  await pressSquare(page, uci.slice(0, 2), pointer);
  await pressSquare(page, uci.slice(2, 4), pointer);
  if (uci.length > 4 && (await page.$('.kr-promo'))) await click(page, '.kr-promo-btn');
}

export async function openSettings(page) {
  await click(page, '.kr-gear');
  await until(async () => (await read(page)).panel, 'the settings dialog', 3000);
}

export async function setSetting(page, key, value) {
  const sel = `[data-key="${key}"]`;
  const kind = await page.$eval(sel, e => e.tagName + ':' + (e.type || ''));
  if (kind.startsWith('SELECT')) await page.select(sel, String(value));
  else if (kind === 'INPUT:checkbox') {
    const checked = await page.$eval(sel, e => e.checked);
    if (checked !== value) await page.click(sel);
  } else if (kind === 'INPUT:range')
    await page.$eval(
      sel,
      (e, v) => {
        e.value = String(v);
        e.dispatchEvent(new Event('input'));
        e.dispatchEvent(new Event('change'));
      },
      value,
    );
  await wait(200);
}

export function panelText(page) {
  return page.$eval('.kr-settings-foot', e => e.innerText.replace(/\n+/g, ' | '));
}

export async function closeSettings(page) {
  await clickText(page, '.kr-settings button', 'Done');
  await until(async () => !(await read(page)).panel, 'the dialog to close', 3000);
  await wait(300);
}

/** Collects uncaught page errors from the moment it is attached. */
export function errorLog(page) {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => m.type() === 'error' && !/favicon|404/.test(m.text()) && errors.push(m.text()));
  return errors;
}

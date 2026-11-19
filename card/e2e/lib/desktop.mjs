// Target: Anki desktop, in an isolated base folder, driven through AnkiConnect + DevTools.
import { homedir } from 'node:os';
import { E2E_DIR, MODEL, puppeteer, SAMPLE_DECK, spawnDetached, until, wait } from './common.mjs';

const env = process.env;
export const DESKTOP = {
  base: env.KR_ANKI_BASE || homedir() + '/.local/share/Anki2-knightly-test',
  profile: env.KR_ANKI_PROFILE || 'Knightly Test',
  port: Number(env.KR_ANKI_PORT || 8766),
  devtools: Number(env.KR_DEVTOOLS_PORT || 9222),
  /** Your own Anki's AnkiConnect; only used to detect that it is running. */
  userPort: Number(env.KR_USER_ANKI_PORT || 8765),
};

export async function ankiConnect(port, action, params = {}, timeout = 15000) {
  const r = await fetch('http://127.0.0.1:' + port, {
    method: 'POST',
    body: JSON.stringify({ action, version: 6, params }),
    signal: AbortSignal.timeout(timeout),
  });
  const j = await r.json();
  if (j.error) throw new Error(action + ': ' + j.error);
  return j.result;
}

async function responds(port) {
  try {
    await ankiConnect(port, 'version', {}, 1500);
    return true;
  } catch (_) {
    return false;
  }
}

export class DesktopTarget {
  name = 'desktop';
  pointer = 'mouse';

  ac(action, params) {
    return ankiConnect(DESKTOP.port, action, params);
  }

  /** Refuse to touch anything but the test profile. */
  async assertSafe() {
    if (DESKTOP.port === DESKTOP.userPort) throw new Error('KR_ANKI_PORT must not be your own AnkiConnect port');
    const p = await this.ac('getActiveProfile');
    if (p !== DESKTOP.profile) throw new Error(`active profile is "${p}", expected "${DESKTOP.profile}"; refusing`);
  }

  async running() {
    return responds(DESKTOP.port);
  }

  /** Start the test Anki (only one Anki can run at a time). */
  async start({ closeUserAnki = false } = {}) {
    if (await this.running()) return 'already running';
    if (await responds(DESKTOP.userPort)) {
      if (!closeUserAnki)
        throw new Error('your own Anki is running; close it first (or pass --close-user-anki to close it cleanly)');
      await ankiConnect(DESKTOP.userPort, 'guiExitAnki');
      await until(async () => !(await responds(DESKTOP.userPort)), 'your Anki to close', 30000);
      await wait(1500);
    }
    spawnDetached(
      'anki',
      ['-b', DESKTOP.base, '-p', DESKTOP.profile],
      { QTWEBENGINE_REMOTE_DEBUGGING: '127.0.0.1:' + DESKTOP.devtools },
      E2E_DIR + 'out/anki-desktop.log',
    );
    await until(() => this.running(), 'the test Anki to start', 60000, 500);
    await this.assertSafe();
    return 'started';
  }

  async stop() {
    if (!(await this.running())) return 'not running';
    await this.assertSafe();
    await this.ac('guiExitAnki');
    await until(async () => !(await this.running()), 'the test Anki to close', 30000);
    return 'stopped';
  }

  async connect() {
    this.browser = await puppeteer.connect({ browserURL: 'http://127.0.0.1:' + DESKTOP.devtools, defaultViewport: null });
  }

  disconnect() {
    if (this.browser) this.browser.disconnect();
    this.browser = undefined;
  }

  /** The reviewer webview. */
  async page() {
    if (!this.browser) await this.connect();
    const pages = await this.browser.pages();
    for (const p of pages) if ((await p.title().catch(() => '')) === 'main webview') return p;
    throw new Error('no reviewer webview');
  }

  /** Show the front of a sample-deck card. */
  async openDeck() {
    await this.ac('guiDeckOverview', { name: SAMPLE_DECK });
    await wait(300);
    if (!(await this.ac('guiDeckReview', { name: SAMPLE_DECK }))) throw new Error('could not open ' + SAMPLE_DECK);
    await until(async () => {
      const c = await this.ac('guiCurrentCard');
      return c && c.deckName === SAMPLE_DECK;
    }, 'a card from ' + SAMPLE_DECK);
    return this.page();
  }

  /** Identity of the card on screen. */
  async currentCard() {
    const c = await this.ac('guiCurrentCard');
    if (!c || c.deckName !== SAMPLE_DECK) throw new Error('the card on screen is not from ' + SAMPLE_DECK);
    return { id: c.cardId, deck: c.deckName };
  }

  async templateConfig() {
    const t = await this.ac('modelTemplates', { modelName: MODEL });
    return Object.values(t).map(x => (x.Front.match(/window\.KR_CONFIG\s*=\s*\{[^{}]*\}/) || [''])[0]);
  }

  async screenshot(page, file) {
    await page.screenshot({ path: file });
  }
}

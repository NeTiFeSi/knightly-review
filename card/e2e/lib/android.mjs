// Target: a separate AnkiDroid test app on a phone, over adb (Wi-Fi) + WebView DevTools.
import { MEDIA } from './deploy.mjs';
import { readFileSync } from 'node:fs';
import { CARD_DIR, loadState, puppeteer, SAMPLE_DECK, saveState, sh, until, wait } from './common.mjs';

const env = process.env;
const REAL_ANKIDROID = 'com.ichi2.anki';
export const ANDROID = {
  /** The test app: an AnkiDroid "parallel" build, never your real AnkiDroid. */
  pkg: env.KR_DROID_PACKAGE || 'com.ichi2.anki.a',
  /** Collection media folder of the test app (where `deploy` puts _knightly.js). */
  media: env.KR_DROID_MEDIA || '/sdcard/AnkiDroid/collection.media',
  devtools: Number(env.KR_DROID_DEVTOOLS_PORT || 9223),
  serial: env.KR_ADB_SERIAL || '',
};
const API = { version: '0.0.3', developer: 'https://github.com/NeTiFeSi/knightly-review' };

export class AndroidTarget {
  name = 'android';
  pointer = 'touch';

  constructor(device) {
    this.serial = device || ANDROID.serial;
  }

  adb(...args) {
    const full = this.serial ? ['-s', this.serial, ...args] : args;
    try {
      return sh('adb', full).trim();
    } catch (e) {
      // Transient transport errors (e.g. "protocol fault") when the phone is reachable
      // both over the fixed port and the paired Wireless-debugging port: retry once.
      if (!/protocol fault|device offline|closed/i.test(String(e.stderr || e.message))) throw e;
      return sh('adb', full).trim();
    }
  }

  shell(cmd) {
    return this.adb('shell', cmd);
  }

  /** Find (or connect to) the phone. `device` may be "ip:port". */
  async ensureDevice() {
    if (this.serial && /:\d+$/.test(this.serial)) {
      const out = sh('adb', ['connect', this.serial]);
      if (/failed|refused|unable/i.test(out)) throw new Error('adb connect ' + this.serial + ': ' + out.trim());
    }
    const list = () =>
      sh('adb', ['devices'])
        .split('\n')
        .slice(1)
        .map(l => l.trim().split(/\s+/))
        .filter(([s, st]) => s && st === 'device')
        .map(([s]) => s);
    let devices = list();
    if (!this.serial && !devices.length) {
      // Wireless debugging advertises itself over mDNS once paired.
      const svc = sh('adb', ['mdns', 'services']).match(/_adb-tls-connect\._tcp\s+([\d.]+:\d+)/);
      if (svc) sh('adb', ['connect', svc[1]]);
      await wait(1000);
      devices = list();
    }
    if (!this.serial) {
      if (!devices.length)
        throw new Error('no phone connected: turn on Wireless debugging and pass --device <ip:port>');
      this.serial = devices.find(d => /:\d+$/.test(d)) || devices[0];
    } else if (!devices.includes(this.serial)) throw new Error('device ' + this.serial + ' is not connected');
    return this.serial;
  }

  /** Refuse to drive anything but the separate test app. */
  async assertSafe() {
    if (ANDROID.pkg === REAL_ANKIDROID) throw new Error('KR_DROID_PACKAGE points at your real AnkiDroid; refusing');
    await this.ensureDevice();
    if (!this.shell('pm list packages ' + ANDROID.pkg).split('\n').some(l => l.trim() === 'package:' + ANDROID.pkg))
      throw new Error(ANDROID.pkg + ' is not installed on ' + this.serial);
  }

  pid() {
    return this.shell('pidof ' + ANDROID.pkg).split(/\s+/)[0] || '';
  }

  topActivity() {
    const m = this.shell('dumpsys activity activities').match(/topResumedActivity=\S+ \S+ (\S+)/);
    return m ? m[1] : '';
  }

  async connect() {
    const pid = await until(() => this.pid(), ANDROID.pkg + ' to run', 15000);
    if (this.browser && this.forwardedPid === pid && this.browser.connected) return;
    this.disconnect();
    try {
      this.adb('forward', '--remove', 'tcp:' + ANDROID.devtools);
    } catch (_) {}
    this.adb('forward', 'tcp:' + ANDROID.devtools, 'localabstract:webview_devtools_remote_' + pid);
    this.forwardedPid = pid;
    this.browser = await until(
      () => puppeteer.connect({ browserURL: 'http://127.0.0.1:' + ANDROID.devtools, defaultViewport: null }),
      "the test app's WebView (is HTML / JavaScript debugging on?)",
      10000,
      500,
    );
  }

  disconnect() {
    if (this.browser) this.browser.disconnect();
    this.browser = undefined;
  }

  /** The study-screen WebView page. */
  async page() {
    await this.connect();
    return until(async () => {
      for (const p of await this.browser.pages()) if (await p.$('#kr-root').catch(() => null)) return p;
      return null;
    }, 'a Knightly card in the study screen');
  }

  /** Call an AnkiDroid JS API function in the card and unwrap {success, value}. */
  async api(page, name) {
    const r = await page.evaluate(
      async (cfg, fn) => {
        const res = await new AnkiDroidJS(cfg)[fn]();
        return typeof res === 'string' ? JSON.parse(res) : res;
      },
      API,
      name,
    );
    if (r && typeof r === 'object' && 'success' in r) {
      if (!r.success) throw new Error(name + ' failed');
      return r.value;
    }
    return r;
  }

  async currentCard(page) {
    page = page || (await this.page());
    const deck = await this.api(page, 'ankiGetDeckName');
    if (deck !== SAMPLE_DECK) throw new Error(`the study screen shows deck "${deck}", not ${SAMPLE_DECK}; refusing`);
    return { id: await this.api(page, 'ankiGetCardId'), deck, did: await this.api(page, 'ankiGetCardDid') };
  }

  /** Tap the on-screen element whose text is `text` (via a UI dump; for native screens). */
  async tapText(text, timeout = 10000) {
    const bounds = await until(() => {
      this.shell('uiautomator dump /sdcard/kr-ui.xml >/dev/null 2>&1');
      const xml = this.shell('cat /sdcard/kr-ui.xml');
      const esc = text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const m = xml.match(new RegExp('text="' + esc + '"[^>]*?bounds="\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]"'));
      return m && m.slice(1).map(Number);
    }, '"' + text + '" on screen', timeout, 700);
    const [x1, y1, x2, y2] = bounds;
    this.shell(`input tap ${Math.round((x1 + x2) / 2)} ${Math.round((y1 + y2) / 2)}`);
  }

  /** Open the sample deck's study screen and return the card page (front). */
  async openDeck() {
    await this.assertSafe();
    const did = loadState().androidDeckId;
    if (did) {
      this.startReview(did);
    } else {
      // First run: find the sample deck. Launching resumes the app where it was; if that is
      // already the study screen, currentCard() below checks it is the sample deck.
      this.shell(`monkey -p ${ANDROID.pkg} -c android.intent.category.LAUNCHER 1`);
      await wait(2000);
      if (!/Reviewer/.test(this.topActivity())) await this.tapText(SAMPLE_DECK, 20000);
    }
    await wait(1500);
    let page = await this.page();
    const card = await this.currentCard(page); // throws on any other deck
    if (!did || String(card.did) !== String(did)) saveState({ androidDeckId: card.did });
    // A previous test may have left the study screen on an answer: start it again for a question.
    for (let i = 0; i < 2 && (await this.side(page)) === 'back'; i++) {
      this.disconnect();
      this.startReview(card.did);
      await wait(1500);
      page = await this.page();
      await this.currentCard(page);
    }
    return page;
  }

  /**
   * Open the study screen for deck `did` through AnkiDroid's review intent. The task is
   * cleared first: otherwise Android only brings the app's existing task to the front
   * ("delivered to top") and the intent is ignored.
   */
  startReview(did) {
    this.disconnect();
    this.shell(`am start --activity-clear-task -n ${ANDROID.pkg}/com.ichi2.anki.IntentHandler --el EXTRA_DECK_ID ${did}`);
  }

  side(page) {
    return page.evaluate(() => (document.getElementById('kr-root') || { dataset: {} }).dataset.side || '');
  }

  /**
   * Fully close and reopen the test app on the sample deck, like a person would: leave it
   * (Home), then close it. Leaving first matters: the WebView only writes cookies to disk
   * when the app goes to the background (or about every 30 s), so killing it straight
   * from the study screen can lose a setting changed a moment before.
   */
  async restartApp() {
    this.disconnect();
    this.shell('input keyevent KEYCODE_HOME');
    await wait(1500);
    this.shell('am force-stop ' + ANDROID.pkg);
    await wait(1000);
    return this.openDeck();
  }

  /** Copy the current build's media (card bundle + built-in engine) into the test collection's media folder. */
  async deploy() {
    await this.assertSafe();
    const real = this.shell(`appops get ${REAL_ANKIDROID} MANAGE_EXTERNAL_STORAGE`);
    if (/allow/.test(real) && !ANDROID.media.includes('/Android/data/' + ANDROID.pkg + '/'))
      throw new Error(
        'your real AnkiDroid has all-files access and may share ' + ANDROID.media + '; set KR_DROID_MEDIA to the test app’s own folder',
      );
    for (const f of MEDIA) readFileSync(CARD_DIR + 'dist/' + f); // fails early if not built
    for (const f of MEDIA) this.adb('push', CARD_DIR + 'dist/' + f, ANDROID.media + '/' + f);
    this.shell('am force-stop ' + ANDROID.pkg); // drop the WebView's copy
    return 'pushed ' + MEDIA.join(', ') + ' to ' + ANDROID.media + ' (templates are not updated: re-import the .apkg for template changes)';
  }

  async screenshot(page, file) {
    await page.screenshot({ path: file });
  }
}

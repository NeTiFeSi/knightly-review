#!/usr/bin/env node
// Captures the README pictures (../../docs/img/*.png) from the card's dev harness in headless
// Chromium, with the sample games in dev/samples.pgn. No Anki and no collection involved, so it
// can be rerun whenever the UI changes:
//
//   npm run build && node e2e/screenshots.mjs [--anki]
//
// With --anki the shots come from the isolated test Anki (node e2e/run.mjs start; deploy), except
// the phone one. Without it the dev harness is used, so no Anki is needed.
//
// The pictures show the arrows the card draws (the best move in green, your rejected move in red,
// the game's own [%cal] arrows) and arrows drawn by hand (right-click drag on the board).
import { createServer } from 'node:http';
import { mkdirSync, readFileSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';
import * as card from './lib/card.mjs';
import { CARD_DIR, puppeteer, SAMPLE_DECK, until as poll, wait } from './lib/common.mjs';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { oracle } from './lib/oracle.mjs';
import { DesktopTarget } from './lib/desktop.mjs';

const REAL = process.argv.includes('--anki'); // capture from the test Anki instead of the dev harness

const OUT = join(CARD_DIR, '..', 'docs', 'img') + '/';
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.wasm': 'application/wasm', '.pgn': 'text/plain' };

function serve() {
  const server = createServer((req, res) => {
    let path = normalize(decodeURIComponent(new URL(req.url, 'http://x').pathname));
    if (path.endsWith('/')) path += 'index.html';
    try {
      const body = readFileSync(join(CARD_DIR, path));
      res.writeHead(200, { 'content-type': TYPES[extname(path)] || 'application/octet-stream' });
      res.end(body);
    } catch (_) {
      res.writeHead(404).end();
    }
  });
  return new Promise(r => server.listen(0, '127.0.0.1', () => r(server)));
}

const SETTINGS = { intro: 'lastMove', afterAnswer: 'stay', autoReplyDelay: 300, hints: true, showEval: true };

/** Right-click drag from one square to another: chessground draws an arrow. */
async function drawArrow(page, from, to, { shift = false } = {}) {
  const board = await page.$('cg-board');
  const box = await board.boundingBox();
  const { orientation } = await card.read(page);
  const centre = sq => {
    const f = sq.charCodeAt(0) - 97;
    const r = Number(sq[1]) - 1;
    const s = box.width / 8;
    const col = orientation === 'white' ? f : 7 - f;
    const row = orientation === 'white' ? 7 - r : r;
    return [box.x + (col + 0.5) * s, box.y + (row + 0.5) * s];
  };
  const [x1, y1] = centre(from);
  const [x2, y2] = centre(to);
  if (shift) await page.keyboard.down('Shift');
  await page.mouse.move(x1, y1);
  await page.mouse.down({ button: 'right' });
  await page.mouse.move((x1 + x2) / 2, (y1 + y2) / 2, { steps: 4 });
  await page.mouse.move(x2, y2, { steps: 4 });
  await page.mouse.up({ button: 'right' });
  if (shift) await page.keyboard.up('Shift');
  await wait(250);
}

/** Record the card while `action` runs; writes docs/img/<name>.gif (8 fps, 560 px wide). */
async function recordWith(getPage, name, action) {
  const dir = mkdtempSync(join(tmpdir(), 'kr-frames-'));
  let n = 0;
  let clip;
  let running = true;
  const loop = (async () => {
    while (running) {
      const t0 = Date.now();
      try {
        const pg = await getPage();
        // One fixed area for every frame (the card grows when the verdict shows), or the GIF is noisy.
        if (!clip) {
          const b = await (await pg.$('.kr-root')).boundingBox();
          clip = { x: Math.floor(b.x), y: Math.floor(b.y), width: Math.floor(b.width), height: 740 };
        }
        await pg.screenshot({ path: join(dir, String(n++).padStart(4, '0') + '.png'), clip });
      } catch (_) {}
      await wait(Math.max(0, 125 - (Date.now() - t0)));
    }
  })();
  try {
    await action();
  } finally {
    running = false;
    await loop;
  }
  const out = OUT + name + '.gif';
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', '8', '-i', join(dir, '%04d.png'), '-vf',
    'scale=560:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4', out]);
  rmSync(dir, { recursive: true });
  console.log(`wrote docs/img/${name}.gif (${Math.round(statSync(out).size / 1024)} KB, ${n} frames)`);
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const { solve } = await oracle();
  const server = REAL ? null : await serve();
  const url = REAL ? '' : `http://127.0.0.1:${server.address().port}/dev/`;
  const anki = REAL ? new DesktopTarget() : null;
  if (anki) await anki.assertSafe();
  const browser = REAL ? null : await puppeteer.launch({
    executablePath: process.env.KR_CHROME || '/usr/bin/chromium',
    headless: true,
    args: ['--no-sandbox'],
    defaultViewport: { width: 1000, height: 880, deviceScaleFactor: 1 },
  });
  const errors = [];
  try {
    const record = (name, action) => recordWith(() => (REAL ? anki.page() : page), name, action);
    if (REAL) {
      // The pictures show card 1: make every sample card new again, so Anki serves them in order (earlier e2e
      // runs grade cards "Again", which leaves another one first).
      const ids = await anki.ac('findCards', { query: `deck:"${SAMPLE_DECK}"` });
      await anki.ac('forgetCards', { cards: ids });
    }
    let page = REAL ? await anki.openDeck() : await browser.newPage();
    page.on('pageerror', e => errors.push(e.message));
    if (!REAL) await page.goto(url, { waitUntil: 'load' });
    await card.waitMounted(page, 'front');

    const sol = solve(await card.pgnText(page), 1);
    const fresh = async (overrides = SETTINGS) => {
      if (REAL) {
        await anki.ac('guiShowQuestion');
        await wait(400);
        page = await anki.page();
      } else await page.evaluate(() => document.getElementById('front').click());
      await card.waitMounted(page, 'front');
      await card.setOverrides(page, overrides);
      await card.waitMounted(page, 'front');
    };
    // Shoot just the card, not the harness bar.
    // `whole`: the card container (the Notes block, with the explorer stats, sits outside `.kr-root`).
    const shot = async (name, whole = false) => {
      if (REAL) page = await anki.page();
      const el = await page.$(REAL && !whole ? '.kr-root' : '#qa');
      await el.screenshot({ path: OUT + name + '.png' });
      console.log('wrote docs/img/' + name + '.png');
    };

    // 1. The front: the puzzle, with arrows drawn by hand.
    await fresh();
    await drawArrow(page, 'c4', 'f7', { shift: true }); // shift: a red arrow
    await drawArrow(page, 'e5', 'f7', { shift: true });
    await shot('front');

    // 2. A hint.
    await fresh();
    await page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => /hint/i.test(x.textContent));
      if (b) b.click();
    });
    await wait(400);
    await shot('hint');

    // 3. A wrong move: your move in red, the best move in green.
    await fresh();
    await card.play(page, sol.rejected.uci, 'mouse');
    await card.waitStatus(page, /✗/);
    await wait(500);
    await shot('wrong-move');

    // 4. The back: graded alternatives with their lines, and the game's own arrows.
    await fresh({ ...SETTINGS, afterAnswer: 'flip', flipDelay: 300 });
    await card.play(page, sol.rejected.uci, 'mouse');
    await until(page);
    await wait(500);
    await shot('back', true);

    // 5. Solved.
    await fresh();
    for (const [i, m] of sol.line.entries()) {
      await card.play(page, m.uci, 'mouse');
      if (i < sol.line.length - 1) await card.waitStatus(page, /keep going/);
      await wait(SETTINGS.autoReplyDelay + 400);
    }
    await card.waitStatus(page, /Solved/);
    await shot('solved');

    // 6. The settings window.
    await fresh();
    await card.openSettings(page);
    await wait(300);
    await shot('card-settings');
    await card.closeSettings(page);

    if (REAL) {
      // 7. The add-on's own windows, saved by the test-only helper add-on (e2e/helper/kr_shots).
      for (const [what, name] of [['settings', 'addon-settings'], ['engine', 'stockfish'], ['progress', 'progress']]) {
        const r = await fetch(`http://127.0.0.1:8767/shot?what=${what}&file=${encodeURIComponent(OUT + name + '.png')}`);
        if (!r.ok) throw new Error('helper add-on failed for ' + what + ': ' + (await r.text()) + ' (see e2e/README.md: kr_shots)');
        console.log('wrote docs/img/' + name + '.png');
      }

      // 9. Short animations.
      await record('draw-arrows', async () => {
        await fresh();
        await wait(600);
        await drawArrow(page, 'c4', 'f7', { shift: true });
        await wait(500);
        await drawArrow(page, 'e5', 'f7', { shift: true });
        await wait(900);
      });
      await record('solve', async () => {
        await fresh();
        await wait(600);
        for (const [i, m] of sol.line.entries()) {
          await card.play(page, m.uci, 'mouse');
          if (i < sol.line.length - 1) await card.waitStatus(page, /keep going/);
          await wait(SETTINGS.autoReplyDelay + 400);
        }
        await wait(900);
      });
      await record('wrong-move', async () => {
        await fresh();
        await wait(600);
        await card.play(page, sol.rejected.uci, 'mouse');
        await card.waitStatus(page, /✗/);
        await wait(1500);
      });
      await fresh();
    }

    // 10. A phone-sized screen, night mode.
    if (REAL) return;
    await page.setViewport({ width: 400, height: 860, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
    await page.evaluate(() => {
      document.getElementById('night').checked = true;
      document.body.classList.add('nightMode');
    });
    await fresh();
    await card.play(page, sol.rejected.uci, 'touch');
    await card.waitStatus(page, /✗/);
    await wait(500);
    await shot('phone');
  } finally {
    if (browser) await browser.close();
    if (server) server.close();
    if (anki) anki.disconnect();
  }
  if (errors.length) {
    console.error('page errors:\n  ' + errors.join('\n  '));
    process.exit(1);
  }
}

/** Wait for the back of the card to show after a flip. */
async function until(page) {
  for (let i = 0; i < 40; i++) {
    const s = await card.read(page);
    if (s.mounted && s.side === 'back') return;
    await wait(250);
  }
  throw new Error('the back never showed');
}

main().catch(e => {
  console.error('error:', e.message);
  process.exit(2);
});

// End-to-end scenarios. Each one opens the sample deck on the front of a card, does
// one thing the way a person would, and checks what the card shows.
// t: target (desktop | android), ctx: { shot(name), solution(page), errors }
import * as card from './lib/card.mjs';
import { expect, until, wait } from './lib/common.mjs';
import { bestMove } from './lib/uci.mjs';

/** Per-device settings used unless a scenario sets its own. Live play is off: these test the PGN part. */
const BASE = { intro: 'lastMove', afterAnswer: 'stay', autoReplyDelay: 300, hints: true, showEval: true, live: false };

async function fresh(t, ctx, overrides = BASE) {
  const page = await t.openDeck();
  ctx.attach(page);
  await card.waitMounted(page, 'front');
  await card.setOverrides(page, overrides);
  await card.waitMounted(page, 'front');
  return page;
}

/** Wait until the study screen shows the back of a card (re-acquiring the page). */
async function waitBack(t, timeout = 10000) {
  return until(async () => {
    const page = await t.page();
    const s = await card.read(page);
    return s.mounted && s.side === 'back' ? page : null;
  }, 'the back of the card', timeout);
}

/** The front clock: text, whether it is paused, low or over, and the bar width in %. */
function clockState(page) {
  return page.evaluate(() => {
    const c = document.querySelector('.kr-clock');
    if (!c) return null;
    const bar = c.querySelector('.kr-clock-bar');
    return {
      text: c.querySelector('.kr-clock-text').textContent,
      paused: c.classList.contains('kr-clock-paused'),
      low: c.classList.contains('kr-clock-low'),
      over: c.classList.contains('kr-clock-over'),
      width: parseFloat(bar.style.width),
    };
  });
}

/** Switch the mounted front to a card whose answer has more than one of your moves; its solution, or null. */
async function multiMoveCard(page, ctx) {
  for (let n = 1; n <= 6; n++) {
    const ok = await page.evaluate(n => {
      const el = document.querySelector('#kr-cloze .cloze');
      if (!el) return false;
      el.setAttribute('data-ordinal', String(n));
      el.textContent = '[' + n + ']';
      window.KnightlyReview.mount();
      return !document.querySelector('.kr-error');
    }, n);
    if (!ok) continue;
    await wait(500);
    const sol = await ctx.solution(page).catch(() => null);
    if (sol && sol.line.length > 1) return sol;
  }
  return null;
}

const TIMED = { ...BASE, timed: true, timeLimit: 60, timeAffects: 'nothing', showClock: true };

export const scenarios = [
  {
    name: 'loads',
    about: 'The front mounts with board, controls and ⚙, and no errors',
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      const s = await card.read(page);
      expect(/to move/.test(s.status), 'status should ask for a move, got: ' + s.status);
      expect(await page.$('.kr-gear'), 'no ⚙ button');
      expect(await page.$('cg-board'), 'no board');
      expect(!s.warning, 'unexpected settings warning: ' + s.warning);
      await ctx.shot('front');
    },
  },
  {
    name: 'solve',
    about: 'Playing the whole accepted line on the board solves the puzzle',
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      const sol = await ctx.solution(page);
      for (const [i, m] of sol.line.entries()) {
        await card.play(page, m.uci, t.pointer);
        if (i < sol.line.length - 1) await card.waitStatus(page, /keep going/);
        await wait(BASE.autoReplyDelay + 400);
      }
      const s = await card.waitStatus(page, /Solved/);
      expect(s.side === 'front', 'should stay on the front with afterAnswer "stay"');
      await ctx.shot('solved');
    },
  },
  {
    name: 'live-play-on',
    about: 'After the PGN line, a live engine keeps the puzzle going until it is settled (desktop: the add-on; elsewhere: built-in Stockfish)',
    async run(t, ctx) {
      const page = await fresh(t, ctx, { ...BASE, live: true });
      const sol = await ctx.solution(page);
      for (const m of sol.line) {
        await card.play(page, m.uci, t.pointer);
        await wait(BASE.autoReplyDelay + 400);
      }
      let s;
      for (let i = 0; i < 15; i++) {
        s = await until(async () => {
          const r = await card.read(page);
          return /thinking|Checking/.test(r.status) ? null : r;
        }, 'the engine', 30000);
        if (/Solved|✗/.test(s.status)) break;
        await wait(300);
        const fen = await page.evaluate(() => document.getElementById('kr-root').getAttribute('data-fen'));
        await card.play(page, await bestMove(fen), t.pointer);
        await wait(BASE.autoReplyDelay + 400);
      }
      expect(/Solved/.test(s.status), 'should end solved, got: ' + s.status);
      const live = await page.$$('.kr-move.kr-live');
      expect(live.length > 0, 'no live moves were played (desktop: the add-on engine; elsewhere: the built-in one)');
      await ctx.shot('live-solved');
    },
  },
  {
    name: 'wrong-move',
    about: "Playing the game's own (bad) move fails and shows the best move",
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      const sol = await ctx.solution(page);
      await card.play(page, sol.rejected.uci, t.pointer);
      const s = await card.waitStatus(page, /✗/);
      expect(s.status.includes(sol.rejected.san), 'status should name ' + sol.rejected.san + ': ' + s.status);
      expect(/Best:/.test(s.status), 'status should list the best move(s)');
      await ctx.shot('failed');
    },
  },
  {
    name: 'nag-badge',
    about: "A move's classification shows as a badge on the board (shown on the front for any visible move, and on the back)",
    async run(t, ctx) {
      let page = await fresh(t, ctx);
      await card.setOverrides(page, { ...BASE, afterAnswer: 'flip', flipDelay: 200 });
      await card.waitMounted(page, 'front');
      await card.click(page, '.kr-giveup');
      const back = await waitBack(t);
      page = back;
      // Step to the game's move in the list.
      await page.evaluate(() => {
        const m = document.querySelector('.kr-move.kr-puzzle-move');
        if (m) m.click();
      });
      await wait(400);
      const glyph = await page.evaluate(() => {
        const m = document.querySelector('.kr-move.kr-puzzle-move .kr-glyph');
        const t = document.querySelector('.cg-custom-svgs text');
        return { list: m && m.textContent, board: t && t.textContent };
      });
      expect(!!glyph.list, 'the puzzle move has no glyph in the list');
      expect(glyph.board === glyph.list, 'board badge ' + glyph.board + ' should equal list glyph ' + glyph.list);
      await ctx.shot('badge');
    },
  },
  {
    name: 'export-menu',
    about: 'Export offers chess.com, Lichess and Copy FEN for the position on the board',
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      await card.click(page, '.kr-export-btn');
      await wait(200);
      const m = await page.evaluate(() => ({
        fen: document.getElementById('kr-root').getAttribute('data-fen'),
        items: [...document.querySelectorAll('.kr-export-item')].map(e => [e.textContent, e.getAttribute('href')]),
      }));
      expect(m.items.length === 3, 'expected 3 menu items, got ' + m.items.length);
      const [chesscom, lichess] = m.items;
      expect(chesscom[1].startsWith('https://www.chess.com/analysis') && chesscom[1].includes(encodeURIComponent(m.fen)), 'chess.com link: ' + chesscom[1]);
      expect(lichess[1] === 'https://lichess.org/analysis/' + m.fen.replace(/ /g, '_'), 'lichess link: ' + lichess[1]);
      await ctx.shot('menu');
      await card.clickText(page, '.kr-export-item', 'Copy FEN');
      await card.waitStatus(page, /./);
      await until(() => page.evaluate(() => /Copied/.test(document.querySelector('.kr-export-item:last-child')?.textContent || '')), 'copy feedback ("Copied", not "Copy failed")', 3000);
    },
  },
  {
    name: 'not-analysed',
    about: 'A legal move that is not in the PGN fails as "not in the analysis"',
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      const sol = await ctx.solution(page);
      if (!sol.unknown) return 'skipped: every legal move is analysed';
      await card.play(page, sol.unknown.uci, t.pointer);
      await card.waitStatus(page, /not in the analysis/);
    },
  },
  {
    name: 'hint',
    about: 'Hint shows the text hint or the piece, and solving reports the hint',
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      const sol = await ctx.solution(page);
      await card.click(page, '.kr-hint');
      await card.waitStatus(page, /💡/);
      await ctx.shot('hint');
      for (const [i, m] of sol.line.entries()) {
        await card.play(page, m.uci, t.pointer);
        await wait(BASE.autoReplyDelay + (i < sol.line.length - 1 ? 500 : 300));
      }
      await card.waitStatus(page, /Solved with 1 hint/);
    },
  },
  {
    name: 'timed-timeout',
    about: 'Timed with timeAffects "fail": when time runs out the card fails and the back says so',
    async run(t, ctx) {
      const timed = { ...BASE, afterAnswer: 'flip', flipDelay: 300, timed: true, timeLimit: 5, timeAffects: 'fail' };
      const page = await fresh(t, ctx, timed);
      expect(await page.$('.kr-clock'), 'no clock on the front');
      await card.waitStatus(page, /Time ran out/, 15000);
      const back = await waitBack(t);
      ctx.attach(back);
      const s = await card.read(back);
      expect(/Time ran out at 5 s/.test(s.status), 'back should say time ran out: ' + s.status);
      await ctx.shot('back-timeout');
    },
  },
  {
    name: 'timed-solved',
    about: 'Timed: solving within the limit shows "Solved in N s of M s" on the back',
    async run(t, ctx) {
      const timed = { ...BASE, afterAnswer: 'flip', flipDelay: 300, timed: true, timeLimit: 120, timeAffects: 'fail' };
      const page = await fresh(t, ctx, timed);
      const sol = await ctx.solution(page);
      for (const [i, m] of sol.line.entries()) {
        await card.play(page, m.uci, t.pointer);
        await wait(BASE.autoReplyDelay + (i < sol.line.length - 1 ? 500 : 300));
      }
      const back = await waitBack(t);
      ctx.attach(back);
      const s = await card.read(back);
      expect(/Solved in [\d.]+ s of 120 s/.test(s.status), 'back should show the time taken: ' + s.status);
      await ctx.shot('back-timed');
    },
  },
  {
    name: 'timed-clock-front',
    about: 'Timed: the clock counts down on the front, turns amber near the end and red when over; showClock off hides it',
    async run(t, ctx) {
      const page = await fresh(t, ctx, { ...TIMED, timeLimit: 10 });
      await until(async () => (await clockState(page)) && !(await clockState(page)).paused, 'the clock to run');
      const a = await clockState(page);
      await wait(2200);
      const b = await clockState(page);
      expect(b.width < a.width, `bar did not shrink: ${a.width} → ${b.width}`);
      expect(parseInt(b.text) < parseInt(a.text), `clock did not count down: ${a.text} → ${b.text}`);
      await until(async () => (await clockState(page)).low, 'the clock to turn amber (last 20%)', 9000);
      await ctx.shot('clock-low');
      await until(async () => (await clockState(page)).over, 'the clock to go over', 9000);
      const o = await clockState(page);
      expect(/over/.test(o.text), 'overtime text expected, got: ' + o.text);
      // timeAffects "nothing": the puzzle is still playable after time is up.
      const s = await card.read(page);
      expect(/to move/.test(s.status), 'puzzle should stay open after time is up: ' + s.status);
      await ctx.shot('clock-over');
      await card.setOverrides(page, { ...TIMED, showClock: false });
      await card.waitMounted(page, 'front');
      expect(!(await page.$('.kr-clock')), 'clock should be hidden with showClock off');
    },
  },
  {
    name: 'timed-over-graded-slow',
    about: 'Timed with timeAffects "nothing": solving after the limit still solves and the back says it was over the limit',
    async run(t, ctx) {
      const page = await fresh(t, ctx, { ...TIMED, afterAnswer: 'flip', flipDelay: 300, timeLimit: 5 });
      const sol = await ctx.solution(page);
      await until(async () => (await clockState(page)).over, 'the time to run out', 12000);
      for (const [i, m] of sol.line.entries()) {
        await card.play(page, m.uci, t.pointer);
        await wait(BASE.autoReplyDelay + (i < sol.line.length - 1 ? 500 : 300));
      }
      const back = await waitBack(t);
      ctx.attach(back);
      const s = await card.read(back);
      expect(/Solved in [\d.]+ s, over the 5 s limit/.test(s.status), 'back should say it was over the limit: ' + s.status);
    },
  },
  {
    name: 'timed-pauses-on-reply',
    about: "Timed: the clock stands still during the opponent's reply delay (uses a card with a multi-move answer)",
    async run(t, ctx) {
      const page = await fresh(t, ctx, { ...TIMED, autoReplyDelay: 3000 });
      const sol = await multiMoveCard(page, ctx);
      if (!sol) return 'skipped: no card in the sample deck has a multi-move answer';
      await wait(1500);
      await card.play(page, sol.line[0].uci, t.pointer);
      await until(async () => (await clockState(page)).paused, 'the clock to pause during the reply');
      const a = await clockState(page);
      await wait(1500);
      const b = await clockState(page);
      expect(a.text === b.text, `clock moved during the reply: ${a.text} → ${b.text}`);
      await until(async () => !(await clockState(page)).paused, 'the clock to resume after the reply', 6000);
    },
  },
  {
    name: 'timed-pauses-for-engine',
    about: 'Timed + live play: the clock stands still whenever the card says the engine is thinking',
    async run(t, ctx) {
      const page = await fresh(t, ctx, { ...TIMED, live: true, autoReplyDelay: 100 });
      const sol = await ctx.solution(page);
      // Record the clock's state at every status change that says the engine is working.
      await page.evaluate(() => {
        window.__krSeen = [];
        new MutationObserver(() => {
          const st = document.querySelector('.kr-status').innerText;
          const c = document.querySelector('.kr-clock');
          if (/thinking|Checking your move/.test(st)) window.__krSeen.push(c.classList.contains('kr-clock-paused'));
        }).observe(document.querySelector('.kr-status'), { childList: true, subtree: true });
      });
      for (const m of sol.line) {
        await card.play(page, m.uci, t.pointer);
        await wait(BASE.autoReplyDelay + 400);
      }
      await until(async () => (await page.evaluate(() => window.__krSeen.length)) > 0, 'the engine wait to show', 30000);
      const seen = await page.evaluate(() => window.__krSeen);
      expect(seen.every(Boolean), 'clock kept running while the engine thought: ' + JSON.stringify(seen));
    },
  },
  {
    name: 'timed-pauses-in-settings',
    about: 'Timed: opening ⚙ freezes the clock',
    async run(t, ctx) {
      const page = await fresh(t, ctx, TIMED);
      await wait(1500);
      await card.openSettings(page);
      await wait(300);
      const a = await clockState(page);
      expect(a && a.paused, 'clock should be paused while ⚙ is open');
      await wait(2000);
      const b = await clockState(page);
      expect(a.text === b.text, `clock moved while ⚙ was open: ${a.text} → ${b.text}`);
      await card.closeSettings(page);
    },
  },
  {
    name: 'timed-settings-after-timeout',
    about: 'Timed: after the time ran out, changing a ⚙ setting starts the puzzle again with a fresh clock, not the spent one',
    async run(t, ctx) {
      const timed = { ...TIMED, timeLimit: 5, timeAffects: 'fail' };
      const page = await fresh(t, ctx, timed);
      await card.waitStatus(page, /Time ran out/, 15000);
      await card.openSettings(page);
      await card.setSetting(page, 'showEval', false); // any change re-mounts the card on close
      await card.closeSettings(page);
      await card.waitMounted(page, 'front');
      await wait(800);
      const s = await card.read(page);
      expect(!/Time ran out/.test(s.status), 'the new attempt failed at once: ' + s.status);
      const c = await clockState(page);
      expect(c && !c.over && parseInt(c.text) >= 3, 'the new attempt did not get a fresh clock: ' + JSON.stringify(c));
      await ctx.shot('fresh-attempt');
    },
  },
  {
    name: 'timed-background',
    targets: ['android'],
    about: 'Timed: leaving the app (Home) pauses the clock; coming back does not time out the puzzle',
    async run(t, ctx) {
      let page = await fresh(t, ctx, { ...TIMED, timeLimit: 10, timeAffects: 'fail' });
      await wait(3000);
      const a = await clockState(page);
      t.disconnect();
      t.shell('input keyevent KEYCODE_HOME');
      await wait(13000); // longer than the whole limit
      t.launch();
      await wait(3000);
      expect(t.studyScreen(), 'the study screen did not come back: ' + t.topActivity());
      page = await t.page();
      ctx.attach(page);
      const s = await card.read(page);
      expect(s.mounted && s.side === 'front', 'card should still be on the front, got side ' + s.side);
      expect(!/Time ran out/.test(s.status), 'time ran out while the app was in the background: ' + s.status);
      const b = await clockState(page);
      // The 13 s away must not count: the clock only ran the few seconds the card was on screen.
      expect(b && !b.over && parseInt(b.text) > 0, `the background time was counted: ${a.text} → ${b && b.text}`);
      await ctx.shot('after-background');
    },
  },
  {
    name: 'give-up-flips',
    about: 'With afterAnswer "flip", giving up shows the back by itself',
    async run(t, ctx) {
      const page = await fresh(t, ctx, { ...BASE, afterAnswer: 'flip', flipDelay: 300 });
      await card.click(page, '.kr-giveup');
      const back = await waitBack(t);
      ctx.attach(back);
      const s = await card.read(back);
      expect(/Answer shown/.test(s.status), 'back should report the answer was shown: ' + s.status);
      await ctx.shot('back');
    },
  },
  {
    name: 'auto-grade',
    about: 'With afterAnswer "grade", the card answers itself (Again) and the next card shows (AnkiDroid new study screen: it says to use the answer buttons, which then grade)',
    async run(t, ctx) {
      // Again keeps the sample card in learning, so the deck never runs dry.
      const grade = { afterAnswer: 'grade', flipDelay: 300, gradeDelay: 800, gradeSolved: 1, gradeHinted: 1, gradeFailed: 1 };
      const page = await fresh(t, ctx, { ...BASE, ...grade });
      const before = await t.currentCard(page);
      await card.click(page, '.kr-giveup');
      const back = await waitBack(t);
      if (t.studyScreen && t.studyScreen() === 'new') {
        // No API grades there: the card must say so (and must not poke the screen's "not supported" dialog), and the native buttons grade.
        await card.waitStatus(back, /grade with the answer buttons/);
        await ctx.shot('note');
        expect(!t.dialogShown(), 'the card poked the study screen into its "features not supported" dialog');
        await t.gradeNative(1);
      } else await card.waitStatus(back, /Marking as Again/);
      await until(
        async () => {
          const p = await t.page();
          const s = await card.read(p);
          if (!s.mounted || s.side !== 'front') return false;
          const now = await t.currentCard(p);
          return String(now.id) !== String(before.id);
        },
        'the next card after grading',
        12000,
      );
      await ctx.shot('next-card');
    },
  },
  {
    name: 'settings-dialog',
    about: '⚙ changes a setting on this device and the card uses it right away',
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      await card.openSettings(page);
      await card.setSetting(page, 'intro', 'fullGame');
      await ctx.shot('dialog');
      // Only differences from the template are stored on the device.
      const stored = await card.storedOverrides(page);
      const templateIntro = await page.evaluate(() => (window.KR_CONFIG || {}).intro || 'lastMove');
      const has = /fullGame/.test(stored.cookie || stored.localStorage || '');
      expect(has === (templateIntro !== 'fullGame'), `stored ${JSON.stringify(stored)} with template intro "${templateIntro}"`);
      await card.closeSettings(page);
      await card.waitStatus(page, /Replaying the game/);
    },
  },
  {
    name: 'settings-survive',
    targets: ['android'],
    about: 'A ⚙ setting survives a full restart of the app',
    async run(t, ctx) {
      let page = await fresh(t, ctx, { ...BASE, intro: 'fullGame' });
      // Seen on AnkiDroid 2.25.3: its new study screen keeps neither the cookie nor localStorage over a restart
      // (each screen is served from a new port, and even "localStorage in the study screen" did not change that).
      if (t.studyScreen && t.studyScreen() === 'new') return 'skipped: the new study screen does not keep page storage across an app restart';
      page = await t.restartApp();
      ctx.attach(page);
      await card.waitMounted(page, 'front');
      const stored = await card.storedOverrides(page);
      expect(/fullGame/.test(stored.cookie || stored.localStorage || ''), 'setting lost after restart: ' + JSON.stringify(stored));
      await card.waitStatus(page, /Replaying the game/);
    },
  },
  {
    name: 'save-all-devices',
    targets: ['desktop'],
    about: '"Save for all devices" (desktop add-on) writes the setting into the templates',
    async run(t, ctx) {
      const page = await fresh(t, ctx);
      const original = await t.templateConfig();
      const want = /intro: 'fullGame'/.test(original[0]) ? 'lastMove' : 'fullGame';
      try {
        await card.openSettings(page);
        await card.setSetting(page, 'intro', want);
        await card.clickText(page, '.kr-settings button', 'Save for all devices');
        await until(async () => /Saved to the card template/.test(await card.panelText(page)), 'the save to finish');
        const now = await t.templateConfig();
        expect(now.every(c => c.includes(`intro: '${want}'`)), 'templates were not updated: ' + now[0]);
        await card.closeSettings(page);
      } finally {
        // Put the test profile's template back as it was.
        const p = await t.page();
        await card.openSettings(p).catch(() => {});
        await card.setSetting(p, 'intro', want === 'fullGame' ? 'lastMove' : 'fullGame').catch(() => {});
        await card.clickText(p, '.kr-settings button', 'Save for all devices').catch(() => {});
        await wait(800);
        await card.closeSettings(p).catch(() => {});
      }
    },
  },
  {
    name: 'bad-setting-warns',
    about: 'An invalid stored setting shows the ⚠ banner instead of failing silently',
    async run(t, ctx) {
      const page = await fresh(t, ctx, { ...BASE, intro: 'fullgame' });
      const s = await card.waitMounted(page, 'front');
      expect(/intro/.test(s.warning), 'expected a warning about intro, got: ' + (s.warning || 'none'));
      await ctx.shot('warning');
    },
  },
];

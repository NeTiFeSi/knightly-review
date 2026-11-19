// End-to-end scenarios. Each one opens the sample deck on the front of a card, does
// one thing the way a person would, and checks what the card shows.
// t: target (desktop | android), ctx: { shot(name), solution(page), errors }
import * as card from './lib/card.mjs';
import { expect, until, wait } from './lib/common.mjs';

/** Per-device settings used unless a scenario sets its own. */
const BASE = { intro: 'lastMove', afterAnswer: 'stay', autoReplyDelay: 300, hints: true, showEval: true };

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
    about: 'With afterAnswer "grade", the card answers itself (Again) and the next card shows',
    async run(t, ctx) {
      // Again keeps the sample card in learning, so the deck never runs dry.
      const grade = { afterAnswer: 'grade', flipDelay: 300, gradeDelay: 800, gradeSolved: 1, gradeHinted: 1, gradeFailed: 1 };
      const page = await fresh(t, ctx, { ...BASE, ...grade });
      const before = await t.currentCard(page);
      await card.click(page, '.kr-giveup');
      const back = await waitBack(t);
      await card.waitStatus(back, /Marking as Again/);
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

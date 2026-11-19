#!/usr/bin/env node
// Knightly Review end-to-end runner for real Anki (desktop) and AnkiDroid (phone).
//
//   node e2e/run.mjs [test] [--target desktop|android|both] [--only a,b] [--device ip:port]
//   node e2e/run.mjs start [--close-user-anki]     start the isolated desktop test Anki
//   node e2e/run.mjs stop                          close it
//   node e2e/run.mjs deploy --target desktop|android   push dist/ into the test profile / test app
//   node e2e/run.mjs deploy --target user [--yes]  push dist/ into YOUR Anki (dry run without --yes)
//   node e2e/run.mjs status [--target ...]         is everything reachable? what card is on screen?
//   node e2e/run.mjs reset --target desktop        make every sample card new again
//   node e2e/run.mjs list                          list scenarios
import * as card from './lib/card.mjs';
import { Fail, outDir, parseArgs, SAMPLE_DECK } from './lib/common.mjs';
import { AndroidTarget } from './lib/android.mjs';
import { deploy } from './lib/deploy.mjs';
import { DESKTOP, DesktopTarget } from './lib/desktop.mjs';
import { oracle } from './lib/oracle.mjs';
import { scenarios } from './scenarios.mjs';

const args = parseArgs(process.argv.slice(2));
const cmd = args._[0] || 'test';

function targets() {
  const which = args.target || 'desktop';
  const list = which === 'both' ? ['desktop', 'android'] : [which];
  return list.map(n => {
    if (n === 'desktop') return new DesktopTarget();
    if (n === 'android') return new AndroidTarget(args.device);
    throw new Error('unknown target ' + n + ' (desktop | android | both)');
  });
}

async function runScenarios(t) {
  const only = args.only ? String(args.only).split(',') : null;
  const list = scenarios.filter(s => (!s.targets || s.targets.includes(t.name)) && (!only || only.includes(s.name)));
  const dir = outDir(t.name);
  const { solve } = await oracle();
  await t.assertSafe();
  const results = [];
  for (const s of list) {
    const ctx = {
      attached: new WeakSet(),
      attach(page) {
        if (this.attached.has(page)) return;
        this.attached.add(page);
        ctx.logs.push(card.errorLog(page));
      },
      logs: [],
      async shot(name) {
        try {
          await t.screenshot(await t.page(), dir + s.name + '-' + name + '.png');
        } catch (_) {
          /* screenshots are best-effort */
        }
      },
      async solution(page) {
        const st = await card.read(page);
        return solve(await card.pgnText(page), st.n);
      },
    };
    const started = Date.now();
    let status = 'PASS';
    let note = '';
    try {
      const r = await s.run(t, ctx);
      if (typeof r === 'string' && r.startsWith('skipped')) {
        status = 'SKIP';
        note = r;
      }
      const pageErrors = ctx.logs.flat();
      if (pageErrors.length) throw new Fail('page errors: ' + pageErrors.join(' / '));
    } catch (e) {
      status = e instanceof Fail ? 'FAIL' : 'ERROR';
      note = e.message;
      await ctx.shot('failure');
    }
    // Leave the device without test settings.
    try {
      await card.clearOverrides(await t.page());
    } catch (_) {}
    const secs = ((Date.now() - started) / 1000).toFixed(1);
    results.push({ target: t.name, scenario: s.name, status, secs, note });
    console.log(`${status.padEnd(5)} ${t.name.padEnd(8)} ${s.name.padEnd(18)} ${secs.padStart(5)}s  ${note}`);
  }
  t.disconnect();
  return results;
}

async function main() {
  if (cmd === 'list') {
    for (const s of scenarios) console.log(`${s.name.padEnd(18)} ${(s.targets || ['desktop', 'android']).join('+').padEnd(16)} ${s.about}`);
    return 0;
  }
  if (cmd === 'start') {
    console.log('desktop test Anki:', await new DesktopTarget().start({ closeUserAnki: !!args['close-user-anki'] }));
    return 0;
  }
  if (cmd === 'stop') {
    console.log('desktop test Anki:', await new DesktopTarget().stop());
    return 0;
  }
  if (cmd === 'deploy') {
    const target = args.target || 'desktop';
    if (target === 'android') {
      console.log(await new AndroidTarget(args.device).deploy());
      return 0;
    }
    if (target === 'user') {
      // Your own Anki: dry run unless --yes.
      const changes = await deploy(DESKTOP.userPort, { apply: !!args.yes });
      if (!changes.length) console.log('your Anki is already up to date');
      else {
        console.log((args.yes ? 'Applied' : 'Would apply (dry run; pass --yes to apply)') + ' to YOUR Anki:');
        changes.forEach(c => console.log('  - ' + c));
        if (args.yes) console.log('Restart Anki (and resync phones) so the reviewers load the new _knightly.js.');
      }
      return 0;
    }
    const t = new DesktopTarget();
    await t.assertSafe();
    const changes = await deploy(DESKTOP.port, { apply: true });
    console.log(changes.length ? 'test profile updated:\n  - ' + changes.join('\n  - ') : 'test profile already up to date');
    return 0;
  }
  if (cmd === 'reset') {
    const t = new DesktopTarget();
    await t.assertSafe();
    const ids = await t.ac('findCards', { query: `deck:"${SAMPLE_DECK}"` });
    await t.ac('forgetCards', { cards: ids });
    console.log('made', ids.length, 'sample cards new again');
    return 0;
  }
  if (cmd === 'status') {
    for (const t of targets()) {
      try {
        await t.assertSafe();
        const page = await t.openDeck();
        const s = await card.waitMounted(page);
        console.log(`${t.name}: OK, card ${s.n} (${s.side}): ${s.status}`);
      } catch (e) {
        console.log(`${t.name}: NOT READY, ${e.message}`);
      } finally {
        t.disconnect();
      }
    }
    return 0;
  }
  if (cmd !== 'test') throw new Error('unknown command ' + cmd);

  const all = [];
  for (const t of targets()) {
    try {
      all.push(...(await runScenarios(t)));
    } catch (e) {
      console.log(`ERROR ${t.name}: ${e.message}`);
      all.push({ target: t.name, scenario: '(setup)', status: 'ERROR', note: e.message });
    }
  }
  const bad = all.filter(r => r.status === 'FAIL' || r.status === 'ERROR');
  console.log(`\n${all.length - bad.length}/${all.length} passed. Screenshots: e2e/out/<target>/`);
  return bad.length ? 1 : 0;
}

main().then(
  code => process.exit(code),
  e => {
    console.error('error:', e.message);
    process.exit(2);
  },
);

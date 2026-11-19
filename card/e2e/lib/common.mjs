// Shared bits for the e2e runner.
import { execFileSync, spawn } from 'node:child_process';
import { mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export { default as puppeteer } from 'puppeteer-core';

export const E2E_DIR = fileURLToPath(new URL('..', import.meta.url));
export const CARD_DIR = fileURLToPath(new URL('../..', import.meta.url));
export const SAMPLE_DECK = process.env.KR_SAMPLE_DECK || 'Knightly Review (sample)';
export const MODEL = 'Knightly Review';

export const wait = ms => new Promise(r => setTimeout(r, ms));

/** Poll `fn` until it returns a truthy value; throws `what` on timeout. */
export async function until(fn, what, timeout = 10000, every = 200) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try {
      last = await fn();
      if (last) return last;
    } catch (e) {
      last = e;
    }
    await wait(every);
  }
  throw new Error('timed out waiting for ' + what + (last instanceof Error ? ' (' + last.message + ')' : ''));
}

export class Fail extends Error {}

export function expect(cond, msg) {
  if (!cond) throw new Fail(msg);
}

export function sh(cmd, args, opts = {}) {
  return execFileSync(cmd, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], ...opts });
}

export function spawnDetached(cmd, args, env, logFile) {
  mkdirSync(dirname(logFile), { recursive: true });
  const out = openSync(logFile, 'a');
  const p = spawn(cmd, args, { env: { ...process.env, ...env }, detached: true, stdio: ['ignore', out, out] });
  p.unref();
  return p.pid;
}

/** Small persistent state (e.g. the test deck id on AnkiDroid), gitignored. */
const STATE_FILE = E2E_DIR + '.state.json';
export function loadState() {
  try {
    return JSON.parse(readFileSync(STATE_FILE, 'utf8'));
  } catch (_) {
    return {};
  }
}
export function saveState(patch) {
  writeFileSync(STATE_FILE, JSON.stringify({ ...loadState(), ...patch }, null, 2) + '\n');
}

export function outDir(target) {
  const d = E2E_DIR + 'out/' + target + '/';
  mkdirSync(d, { recursive: true });
  return d;
}

export function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const [k, v] = a.slice(2).split('=');
      if (v !== undefined) args[k] = v;
      else if (argv[i + 1] && !argv[i + 1].startsWith('--')) args[k] = argv[++i];
      else args[k] = true;
    } else args._.push(a);
  }
  return args;
}

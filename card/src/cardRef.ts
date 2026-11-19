// Reads what the Anki template rendered: active cloze number, PGN text, config.
import { DEFAULT_ACCEPT } from './nag';
import type { Side } from './pgn';

export type Intro = 'lastMove' | 'fullGame';
export type AfterAnswer = 'flip' | 'stay' | 'grade';

export interface Config {
  /** NAGs counted as correct (1 !, 3 !!, 5 !?). */
  acceptNags: number[];
  /** ms before the opponent's reply is played. */
  autoReplyDelay: number;
  showEval: boolean;
  evalOnPuzzleFront: boolean;
  /** Front opening: animate only the last move, or replay the game up to the puzzle. */
  intro: Intro;
  /** ms per move when intro is 'fullGame'. */
  introMoveDelay: number;
  /** When the attempt ends: flip to the back, stay on the front, or flip and grade automatically. */
  afterAnswer: AfterAnswer;
  /** ms to show the result before flipping. */
  flipDelay: number;
  /** ms on the back before the automatic grade (any tap or key cancels it). */
  gradeDelay: number;
  /** Eases used by afterAnswer 'grade' (1 Again, 2 Hard, 3 Good, 4 Easy). */
  gradeSolved: number;
  gradeHinted: number;
  gradeFailed: number;
  /** Show the "Hint" button. */
  hints: boolean;
}

export const DEFAULT_CONFIG: Config = {
  acceptNags: DEFAULT_ACCEPT,
  autoReplyDelay: 400,
  showEval: true,
  evalOnPuzzleFront: false,
  intro: 'lastMove',
  introMoveDelay: 900,
  afterAnswer: 'flip',
  flipDelay: 1200,
  gradeDelay: 2500,
  gradeSolved: 3,
  gradeHinted: 2,
  gradeFailed: 1,
  hints: true,
};

declare global {
  interface Window {
    /** Set by the template; null when its config script failed to run. */
    KR_CONFIG?: Partial<Config> | null;
  }
}

const CHOICES: Partial<Record<keyof Config, readonly string[]>> = {
  intro: ['lastMove', 'fullGame'],
  afterAnswer: ['flip', 'stay', 'grade'],
};

/** [min, max, integer] for numeric settings. */
const RANGES: Partial<Record<keyof Config, [number, number, boolean]>> = {
  autoReplyDelay: [0, 10000, false],
  introMoveDelay: [100, 10000, false],
  flipDelay: [0, 30000, false],
  gradeDelay: [0, 60000, false],
  gradeSolved: [1, 4, true],
  gradeHinted: [1, 4, true],
  gradeFailed: [1, 4, true],
};

export const CONFIG_KEYS = Object.keys(DEFAULT_CONFIG) as (keyof Config)[];

function show(v: unknown): string {
  return typeof v === 'string' ? "'" + v + "'" : JSON.stringify(v);
}

/** Why `v` is not a valid value for `k`, or undefined when it is. */
function problem(k: keyof Config, v: unknown): string | undefined {
  const d = DEFAULT_CONFIG[k];
  if (Array.isArray(d)) {
    if (!Array.isArray(v) || !v.every(x => typeof x === 'number' && x === Math.floor(x) && x > 0))
      return 'expected a list of NAG numbers like [1, 3, 5]';
    return undefined;
  }
  if (typeof d === 'boolean') return typeof v === 'boolean' ? undefined : 'expected true or false';
  if (typeof d === 'number') {
    if (typeof v !== 'number' || !isFinite(v)) return 'expected a number';
    const r = RANGES[k];
    if (r && (v < r[0] || v > r[1] || (r[2] && v !== Math.floor(v))))
      return 'expected ' + (r[2] ? 'a whole number' : 'a number') + ' from ' + r[0] + ' to ' + r[1];
    return undefined;
  }
  const allowed = CHOICES[k];
  if (typeof v !== 'string' || (allowed && allowed.indexOf(v) < 0))
    return 'expected ' + (allowed ? allowed.map(show).join(' or ') : 'text');
  return undefined;
}

export interface ConfigCheck {
  config: Config;
  /** The valid entries of `raw`. */
  valid: Partial<Config>;
  warnings: string[];
}

/**
 * Validates `raw` field by field on top of `base`. Invalid or unknown entries
 * are dropped with a warning; everything else falls back to `base`.
 */
export function checkConfig(raw: unknown, base: Config = DEFAULT_CONFIG, source = 'KR_CONFIG'): ConfigCheck {
  const out = { ...base } as Record<string, unknown>;
  const valid: Record<string, unknown> = {};
  const warnings: string[] = [];
  if (raw === undefined) return { config: base, valid: {}, warnings };
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    warnings.push(source + ' could not be read (is there a typo in the template?) — using defaults.');
    return { config: base, valid: {}, warnings };
  }
  const c = raw as Record<string, unknown>;
  Object.keys(c).forEach(key => {
    const v = c[key];
    if (CONFIG_KEYS.indexOf(key as keyof Config) < 0) {
      warnings.push(source + ': unknown setting "' + key + '" (ignored).');
      return;
    }
    const k = key as keyof Config;
    if (v === undefined) return;
    const why = problem(k, v);
    if (why) {
      warnings.push(source + ': ' + k + ' = ' + show(v) + ' is not valid, ' + why + ' — using ' + show(base[k]) + '.');
      return;
    }
    out[k] = v;
    valid[k] = v;
  });
  return { config: out as unknown as Config, valid: valid as Partial<Config>, warnings };
}

/** KR_CONFIG from the template, validated field by field against the defaults. */
export function readConfig(raw?: Partial<Config>): Config {
  return checkConfig(raw || (typeof window !== 'undefined' && window.KR_CONFIG) || {}).config;
}

/** Entries of `cfg` that differ from `base`. */
export function configDiff(cfg: Config, base: Config): Partial<Config> {
  const out: Record<string, unknown> = {};
  CONFIG_KEYS.forEach(k => {
    if (JSON.stringify(cfg[k]) !== JSON.stringify(base[k])) out[k] = cfg[k];
  });
  return out as Partial<Config>;
}

/** `window.KR_CONFIG = {…};` in the template's style, ready to paste. */
export function formatConfig(cfg: Config): string {
  const lines = CONFIG_KEYS.map(k => {
    const v = cfg[k];
    const s = Array.isArray(v) ? '[' + v.join(', ') + ']' : typeof v === 'string' ? "'" + v + "'" : String(v);
    return '  ' + k + ': ' + s;
  });
  return 'window.KR_CONFIG = {\n' + lines.join(',\n') + '\n};';
}

/**
 * Field HTML → plain PGN text. Anki's editor stores line breaks as <br>/<div>
 * and escapes entities; both are undone here.
 */
export function fieldHtmlToText(html: string): string {
  const withBreaks = html
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(div|p)>/gi, '\n')
    .replace(/<[^>]*>/g, '');
  const ta = document.createElement('textarea');
  ta.innerHTML = withBreaks;
  return ta.value.replace(/ /g, ' ');
}

/**
 * Active cloze ordinal. Front renders `<span class="cloze" data-ordinal="N">[N]</span>`
 * (older clients omit data-ordinal), back renders the answer text `N`.
 */
export function clozeOrdinal(el: ParentNode): number | undefined {
  const span = el.querySelector('.cloze');
  if (!span) return undefined;
  const attr = span.getAttribute('data-ordinal');
  if (attr && /^\d+$/.test(attr)) return parseInt(attr, 10);
  const m = /\d+/.exec(span.textContent || '');
  return m ? parseInt(m[0], 10) : undefined;
}

export interface CardRef {
  side: Side;
  n: number;
  pgn: string;
}

export function readCard(root: HTMLElement, doc: ParentNode = document): CardRef | string {
  const side: Side = root.getAttribute('data-side') === 'back' ? 'back' : 'front';
  const clozeEl = doc.querySelector('#kr-cloze');
  const pgnEl = doc.querySelector('#kr-pgn');
  if (!clozeEl || !pgnEl) return 'Template is missing #kr-cloze or #kr-pgn.';
  const n = clozeOrdinal(clozeEl);
  if (n === undefined) return 'Could not find the active cloze in the Cards field.';
  return { side, n, pgn: fieldHtmlToText(pgnEl.innerHTML) };
}

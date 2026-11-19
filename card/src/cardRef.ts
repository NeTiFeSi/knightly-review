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
    KR_CONFIG?: Partial<Config>;
  }
}

const CHOICES: Partial<Record<keyof Config, readonly string[]>> = {
  intro: ['lastMove', 'fullGame'],
  afterAnswer: ['flip', 'stay', 'grade'],
};

/** KR_CONFIG from the template, validated field by field against the defaults. */
export function readConfig(raw?: Partial<Config>): Config {
  const c = (raw || (typeof window !== 'undefined' && window.KR_CONFIG) || {}) as Record<string, unknown>;
  const out = { ...DEFAULT_CONFIG } as Record<string, unknown>;
  (Object.keys(DEFAULT_CONFIG) as (keyof Config)[]).forEach(k => {
    const v = c[k];
    const d = DEFAULT_CONFIG[k];
    if (v === undefined) return;
    if (Array.isArray(d)) {
      if (Array.isArray(v) && v.every(x => typeof x === 'number')) out[k] = v;
    } else if (typeof v === typeof d) {
      const allowed = CHOICES[k];
      if (!allowed || allowed.indexOf(v as string) >= 0) out[k] = v;
    }
  });
  return out as unknown as Config;
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

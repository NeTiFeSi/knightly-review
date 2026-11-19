// Reads what the Anki template rendered: active cloze number, PGN text, config.
import { DEFAULT_ACCEPT } from './nag';
import type { Side } from './pgn';

export interface Config {
  acceptNags: number[];
  autoReplyDelay: number;
  showEval: boolean;
  evalOnPuzzleFront: boolean;
}

export const DEFAULT_CONFIG: Config = {
  acceptNags: DEFAULT_ACCEPT,
  autoReplyDelay: 400,
  showEval: true,
  evalOnPuzzleFront: false,
};

declare global {
  interface Window {
    KR_CONFIG?: Partial<Config>;
  }
}

export function readConfig(): Config {
  const c = (typeof window !== 'undefined' && window.KR_CONFIG) || {};
  return {
    acceptNags: Array.isArray(c.acceptNags) ? c.acceptNags : DEFAULT_CONFIG.acceptNags,
    autoReplyDelay: typeof c.autoReplyDelay === 'number' ? c.autoReplyDelay : DEFAULT_CONFIG.autoReplyDelay,
    showEval: typeof c.showEval === 'boolean' ? c.showEval : DEFAULT_CONFIG.showEval,
    evalOnPuzzleFront:
      typeof c.evalOnPuzzleFront === 'boolean' ? c.evalOnPuzzleFront : DEFAULT_CONFIG.evalOnPuzzleFront,
  };
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

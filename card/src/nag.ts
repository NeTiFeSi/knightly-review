// Move-quality NAGs ($1–$6) and the accept/reject rules for answers.

export interface NagInfo {
  glyph: string;
  label: string;
  cls: string;
}

export const NAGS: Record<number, NagInfo> = {
  1: { glyph: '!', label: 'Good', cls: 'good' },
  2: { glyph: '?', label: 'Mistake', cls: 'mistake' },
  3: { glyph: '!!', label: 'Brilliant', cls: 'brilliant' },
  4: { glyph: '??', label: 'Blunder', cls: 'blunder' },
  5: { glyph: '!?', label: 'Interesting', cls: 'interesting' },
  6: { glyph: '?!', label: 'Dubious', cls: 'dubious' },
};

export const DEFAULT_ACCEPT = [1, 3, 5];

/** The first move-quality NAG of a move, if any. */
export function moveNag(nags: readonly number[]): number | undefined {
  return nags.find(n => n in NAGS);
}

export function nagInfo(nags: readonly number[]): NagInfo | undefined {
  const n = moveNag(nags);
  return n === undefined ? undefined : NAGS[n];
}

export function isAccepted(nags: readonly number[], accept: readonly number[]): boolean {
  return nags.some(n => accept.indexOf(n) >= 0);
}

export function isRejected(nags: readonly number[], accept: readonly number[]): boolean {
  return nags.some(n => n in NAGS && accept.indexOf(n) < 0);
}

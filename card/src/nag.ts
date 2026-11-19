// Move-quality NAGs ($1–$6, and $8 singular move) and the accept/reject rules for answers.
// The generator's meaning: !! the only good move and a sacrifice; □ ($8) the only good move;
// !? a good sacrifice when another good move keeps more material; ! any other good move;
// ?! / ? / ?? lose up to 10 %, 10–20 %, over 20 % of the expected score.

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
  8: { glyph: '□', label: 'Only move', cls: 'singular' },
};

export const DEFAULT_ACCEPT = [1, 3, 5, 8];

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

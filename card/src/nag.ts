// Move-quality NAGs ($1–$6, and $8 singular move) and the accept/reject rules for answers.
// The generator's meaning: !! the only good move and a sacrifice; □ ($8) the only good move;
// !? a good sacrifice when another good move keeps more material; ! any other good move;
// ?! / ? / ?? lose 5–10 %, 10–20 %, over 20 % of the expected score;
// a move between a good one and ?! (about 1–5 %) has no NAG: normal play, not accepted by default.

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

/** Marks "moves with no NAG" (normal play) in an accept list. */
export const NEUTRAL = 0;

/** The accept list for the settings: with `neutral`, unmarked moves count too. */
export function acceptList(cfg: { acceptNags: readonly number[]; acceptNeutral: boolean }): readonly number[] {
  return cfg.acceptNeutral ? [...cfg.acceptNags, NEUTRAL] : cfg.acceptNags;
}

/** The first move-quality NAG of a move, if any. */
export function moveNag(nags: readonly number[]): number | undefined {
  return nags.find(n => n in NAGS);
}

export function nagInfo(nags: readonly number[]): NagInfo | undefined {
  const n = moveNag(nags);
  return n === undefined ? undefined : NAGS[n];
}

export function isAccepted(nags: readonly number[], accept: readonly number[]): boolean {
  if (accept.indexOf(NEUTRAL) >= 0 && moveNag(nags) === undefined) return true;
  return nags.some(n => accept.indexOf(n) >= 0);
}

export function isRejected(nags: readonly number[], accept: readonly number[]): boolean {
  return nags.some(n => n in NAGS && accept.indexOf(n) < 0);
}

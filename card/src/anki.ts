// Bridge to the reviewer: show the answer / answer the card, where the client allows it. The card never
// flips or grades by itself: it only asks the host to, and says so when the host can't.
// Desktop: pycmd('ans') / pycmd('easeN') (easeN only works on the answer side).
// AnkiDroid, old reviewer: the globals showAnswer() / buttonAnswerEaseN().
// AnkiDroid, new study screen (2.25+): showAnswer() works, but buttonAnswerEaseN() only raises its
//   "features not yet supported" dialog (every signal: URL does), so grading is left to its own buttons.
// AnkiMobile / AnkiWeb: no hook — callers fall back to manual flipping/grading.

export type Ease = 1 | 2 | 3 | 4;

type Fn = (...args: unknown[]) => unknown;
interface Win {
  pycmd?: Fn;
  showAnswer?: Fn;
  ankiPlatform?: string;
  [k: string]: unknown;
}

function w(): Win {
  return window as unknown as Win;
}

function call(fn: unknown, ...args: unknown[]): boolean {
  if (typeof fn !== 'function') return false;
  try {
    (fn as Fn)(...args);
    return true;
  } catch (_) {
    return false;
  }
}

/** AnkiDroid's new study screen: it runs Anki's own reviewer scripts, which set `ankiPlatform`. */
export function onNewStudyScreen(): boolean {
  return w().ankiPlatform === 'ankidroid';
}

/** Flip to the answer side. Returns false when the client has no way to do it. */
export function showAnswer(): boolean {
  const win = w();
  return call(win.pycmd, 'ans') || call(win.showAnswer);
}

/** Answer the card (from the answer side). Returns false when unsupported. */
export function answerCard(ease: Ease): boolean {
  if (!canAnswerCard()) return false;
  const win = w();
  return call(win.pycmd, 'ease' + ease) || call(win['buttonAnswerEase' + ease]);
}

/** Can the card flip by itself? */
export function canControlReviewer(): boolean {
  const win = w();
  return typeof win.pycmd === 'function' || typeof win.showAnswer === 'function';
}

/** Can the card grade by itself? (Not on AnkiDroid's new study screen: use its answer buttons.) */
export function canAnswerCard(): boolean {
  const win = w();
  if (typeof win.pycmd === 'function') return true;
  return !onNewStudyScreen() && typeof win.buttonAnswerEase1 === 'function';
}

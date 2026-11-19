// Bridge to the reviewer: show the answer / answer the card, where the client allows it.
// Desktop: pycmd('ans') / pycmd('easeN') (easeN only works on the answer side).
// AnkiDroid: legacy globals showAnswer() / buttonAnswerEaseN(), else the AnkiDroidJS API.
// AnkiMobile / AnkiWeb: no hook — callers fall back to manual flipping/grading.

export type Ease = 1 | 2 | 3 | 4;

type Fn = (...args: unknown[]) => unknown;
interface Win {
  pycmd?: Fn;
  showAnswer?: Fn;
  AnkiDroidJS?: new (contract: { version: string; developer: string }) => Record<string, Fn>;
  [k: string]: unknown;
}

let droid: Record<string, Fn> | undefined;

function w(): Win {
  return window as unknown as Win;
}

function droidApi(): Record<string, Fn> | undefined {
  if (droid) return droid;
  const Ctor = w().AnkiDroidJS;
  if (typeof Ctor !== 'function') return undefined;
  try {
    droid = new Ctor({ version: '0.0.3', developer: 'https://github.com/NeTiFeSi/knightly-review' });
  } catch (_) {
    droid = undefined;
  }
  return droid;
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

/** Flip to the answer side. Returns false when the client has no way to do it. */
export function showAnswer(): boolean {
  const win = w();
  if (call(win.pycmd, 'ans')) return true;
  if (call(win.showAnswer)) return true;
  const api = droidApi();
  return !!api && call(api.ankiShowAnswer && api.ankiShowAnswer.bind(api));
}

/** Answer the card (from the answer side). Returns false when unsupported. */
export function answerCard(ease: Ease): boolean {
  const win = w();
  if (call(win.pycmd, 'ease' + ease)) return true;
  if (call(win['buttonAnswerEase' + ease])) return true;
  const api = droidApi();
  const m = api && api['ankiAnswerEase' + ease];
  return !!api && call(m && m.bind(api));
}

export function canControlReviewer(): boolean {
  const win = w();
  return typeof win.pycmd === 'function' || typeof win.showAnswer === 'function' || !!droidApi();
}

// Carries the front's result to the back side across Anki clients.

export interface StoredResult {
  key: string;
  status: 'solved' | 'failed';
  /** Node ids of the user's accepted moves. */
  played: string[];
  reason?: 'rejected' | 'unknown' | 'gaveup' | 'live' | 'timeout';
  /** Position id where the attempt failed. */
  failAt?: string;
  failSan?: string;
  /** UCI of the failed move (drawn as an arrow on the back). */
  failUci?: string;
  /** Node id of the rejected move, when it is in the PGN. */
  failNode?: string;
  /** Live play: node id where it started, the engine UCIs played (failed move last), and why it ended. */
  liveFrom?: string;
  live?: string[];
  liveEnd?: string;
  /** Live failure: the engine's best move (UCI) and its SAN. */
  failBest?: string;
  failBestSan?: string;
  /** Live failure in a forced mate: the best mate, and the failed move's (absent when it dropped the mate). */
  failBestMate?: number;
  failMate?: number;
  /** Hints revealed during the attempt. */
  hints?: number;
  /** Timed puzzles: your thinking time in ms, and whether it went over the limit. */
  elapsedMs?: number;
  timedOut?: boolean;
  /** Set once the card was graded automatically, so it is not graded twice. */
  graded?: boolean;
}

declare global {
  interface Window {
    __krResults?: Record<string, StoredResult>;
  }
}

/** FNV-1a, enough to tell cards apart. */
export function cardKey(pgn: string, n: number): string {
  let h = 0x811c9dc5;
  const s = pgn + '\u0000' + n;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(36) + ':' + n;
}

const PREFIX = 'kr:';

export function saveResult(r: StoredResult): void {
  try {
    (window.__krResults = window.__krResults || {})[r.key] = r;
  } catch (_) {
    /* ignore */
  }
  try {
    sessionStorage.setItem(PREFIX + r.key, JSON.stringify(r));
  } catch (_) {
    /* storage may be blocked */
  }
}

export function loadResult(key: string): StoredResult | undefined {
  try {
    const r = window.__krResults && window.__krResults[key];
    if (r) return r;
  } catch (_) {
    /* ignore */
  }
  try {
    const raw = sessionStorage.getItem(PREFIX + key);
    if (raw) {
      const r = JSON.parse(raw) as StoredResult;
      if (r && r.key === key) return r;
    }
  } catch (_) {
    /* ignore */
  }
  return undefined;
}

/** Forget a result once a new review of the same card starts. */
export function clearResult(key: string): void {
  try {
    if (window.__krResults) delete window.__krResults[key];
  } catch (_) {
    /* ignore */
  }
  try {
    sessionStorage.removeItem(PREFIX + key);
  } catch (_) {
    /* ignore */
  }
}

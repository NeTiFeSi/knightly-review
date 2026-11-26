// Which opponent replies this card has shown before, so the next review shows one that was seen less.
// Counts are per card (not per device) as far as card JavaScript allows: they are kept in this device's
// browser storage, and merged with the counts the desktop add-on saved in the note's `Visits` field, which
// syncs to every device (see addon/knightly_review/__init__.py).
import type { Visits } from './puzzle';

const KEY = 'kr:visits:v1';
const COOKIE = 'kr_visits';

type Store = Record<string, Record<string, number>>;

function readLocal(): Store {
  let raw: string | null | undefined;
  try {
    raw = localStorage.getItem(KEY);
  } catch (_) {
    raw = undefined;
  }
  if (!raw) {
    try {
      const m = new RegExp('(?:^|;\\s*)' + COOKIE + '=([^;]*)').exec(document.cookie);
      raw = m ? decodeURIComponent(m[1]) : undefined;
    } catch (_) {
      raw = undefined;
    }
  }
  try {
    return raw ? (JSON.parse(raw) as Store) : {};
  } catch (_) {
    return {};
  }
}

function writeLocal(store: Store): void {
  const json = JSON.stringify(store);
  try {
    localStorage.setItem(KEY, json);
  } catch (_) {
    /* blocked */
  }
  try {
    if (json.length < 3000) document.cookie = COOKIE + '=' + encodeURIComponent(json) + '; max-age=315360000; path=/; SameSite=Lax';
  } catch (_) {
    /* cookies may be blocked */
  }
}

/** The counts synced in the note's hidden `Visits` element: JSON `{fenKey: n}` for this card, if any. */
export function syncedVisits(root: Document = document): Record<string, number> {
  const el = root.getElementById('kr-visits');
  if (!el) return {};
  const ta = root.createElement('textarea');
  ta.innerHTML = (el.innerHTML || '').replace(/<[^>]*>/g, '');
  try {
    const all = JSON.parse(ta.value || '{}') as Record<string, Record<string, number>>;
    return all && typeof all === 'object' ? (all as never) : {};
  } catch (_) {
    return {};
  }
}

export function mergeCounts(...parts: Array<Record<string, number> | undefined>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const p of parts) if (p) for (const k of Object.keys(p)) out[k] = Math.max(out[k] || 0, p[k]);
  return out;
}

/** The visit counts of the card with this key. `onChange` is told the new counts (the add-on saves them). */
export function cardVisits(cardKey: string, onChange?: (counts: Record<string, number>) => void): Visits {
  const local = readLocal();
  const synced = syncedVisits() as unknown as Record<string, Record<string, number>>;
  const counts = mergeCounts(synced[cardKey], local[cardKey]);
  return {
    get: k => counts[k] || 0,
    add: k => {
      counts[k] = (counts[k] || 0) + 1;
      const store = readLocal();
      store[cardKey] = counts;
      writeLocal(store);
      (onChange || ((c: Record<string, number>) => syncToAddon(cardKey, c)))(counts);
    },
  };
}

function syncToAddon(cardKey: string, counts: Record<string, number>): void {
  const w = window as unknown as { pycmd?: (cmd: string) => unknown };
  if (typeof w.pycmd === 'function') {
    try {
      w.pycmd('kr:visits:' + cardKey + '|' + JSON.stringify(counts));
    } catch (_) {
      /* not on desktop, or the add-on is missing */
    }
  }
}

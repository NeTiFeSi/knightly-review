// Per-device settings (overrides on top of the template's KR_CONFIG) and the
// optional desktop add-on that writes settings into the template itself.
import type { Config } from './cardRef';

const KEY = 'kr:settings:v1';
const COOKIE = 'kr_settings';

function readCookie(): string | undefined {
  try {
    const m = new RegExp('(?:^|;\\s*)' + COOKIE + '=([^;]*)').exec(document.cookie);
    return m ? decodeURIComponent(m[1]) : undefined;
  } catch (_) {
    return undefined;
  }
}

function writeCookie(value: string | undefined): void {
  try {
    document.cookie =
      COOKIE +
      '=' +
      (value === undefined ? '; max-age=0' : encodeURIComponent(value) + '; max-age=315360000') +
      '; path=/; SameSite=Lax';
  } catch (_) {
    /* cookies may be blocked */
  }
}

/** Raw per-device overrides (validated by the caller). */
export function loadOverrides(): unknown {
  let raw: string | null | undefined;
  try {
    raw = localStorage.getItem(KEY);
  } catch (_) {
    raw = undefined;
  }
  if (!raw) raw = readCookie();
  if (!raw) return undefined;
  try {
    return JSON.parse(raw);
  } catch (_) {
    return undefined;
  }
}

/** Store overrides; an empty object clears them. Returns false if nothing could store them. */
export function saveOverrides(o: Partial<Config>): boolean {
  if (!Object.keys(o).length) {
    clearOverrides();
    return true;
  }
  const json = JSON.stringify(o);
  let ok = false;
  try {
    localStorage.setItem(KEY, json);
    ok = localStorage.getItem(KEY) === json;
  } catch (_) {
    /* blocked */
  }
  writeCookie(json);
  return ok || readCookie() === json;
}

export function clearOverrides(): void {
  try {
    localStorage.removeItem(KEY);
  } catch (_) {
    /* blocked */
  }
  writeCookie(undefined);
}

/** Whether this webview can store settings at all. */
export function storageWorks(): boolean {
  const probe = 'kr:probe';
  try {
    localStorage.setItem(probe, '1');
    const ok = localStorage.getItem(probe) === '1';
    localStorage.removeItem(probe);
    if (ok) return true;
  } catch (_) {
    /* try cookies */
  }
  try {
    document.cookie = 'kr_probe=1; path=/; SameSite=Lax';
    const ok = /(?:^|;\s*)kr_probe=1/.test(document.cookie);
    document.cookie = 'kr_probe=; max-age=0; path=/; SameSite=Lax';
    return ok;
  } catch (_) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Desktop add-on bridge (addon/knightly_review): pycmd('kr:…', callback).

type PyCmd = (cmd: string, cb?: (r: unknown) => void) => unknown;

function pycmdFn(): PyCmd | undefined {
  const w = window as unknown as { pycmd?: PyCmd };
  return typeof w.pycmd === 'function' ? w.pycmd : undefined;
}

/** True on Anki desktop, where browser storage is wiped on restart. */
export function isDesktop(): boolean {
  return !!pycmdFn();
}

function call(cmd: string, timeout = 1500): Promise<unknown> {
  const py = pycmdFn();
  if (!py) return Promise.resolve(undefined);
  return new Promise(resolve => {
    const t = window.setTimeout(() => resolve(undefined), timeout);
    try {
      py(cmd, r => {
        clearTimeout(t);
        resolve(r);
      });
    } catch (_) {
      clearTimeout(t);
      resolve(undefined);
    }
  });
}

let addonCache: Promise<boolean> | undefined;

/** Whether the Knightly Review desktop add-on answers. Cached per page. */
export function addonAvailable(): Promise<boolean> {
  if (!addonCache)
    addonCache = call('kr:hello').then(r => !!r && typeof r === 'object' && (r as { kr?: unknown }).kr === 1);
  return addonCache;
}

export interface SaveReply {
  ok: boolean;
  error?: string;
}

/** Ask the add-on to write `cfg` into this card's note type (front and back). */
export function saveToTemplate(cfg: Config): Promise<SaveReply> {
  return call('kr:config:' + JSON.stringify(cfg), 5000).then(r => {
    if (r && typeof r === 'object' && (r as SaveReply).ok === true) return { ok: true };
    const err = r && typeof r === 'object' ? (r as SaveReply).error : undefined;
    return { ok: false, error: err || 'The add-on did not answer.' };
  });
}

import { beforeEach, describe, expect, it } from 'vitest';
import { clearOverrides, loadOverrides, saveOverrides, storageWorks } from '../src/settings';

class MemStorage {
  m = new Map<string, string>();
  getItem(k: string) { return this.m.has(k) ? this.m.get(k)! : null; }
  setItem(k: string, v: string) { this.m.set(k, v); }
  removeItem(k: string) { this.m.delete(k); }
}

/** Minimal document.cookie: set one "name=value; attrs" at a time, max-age=0 deletes. */
function cookieDoc() {
  const jar = new Map<string, string>();
  return {
    get cookie() { return [...jar].map(([k, v]) => k + '=' + v).join('; '); },
    set cookie(s: string) {
      const [pair, ...attrs] = s.split(';');
      const i = pair.indexOf('=');
      const k = pair.slice(0, i).trim();
      if (attrs.some(a => a.trim() === 'max-age=0')) jar.delete(k);
      else jar.set(k, pair.slice(i + 1));
    },
  };
}

const g = globalThis as any;
beforeEach(() => {
  g.localStorage = new MemStorage();
  g.document = cookieDoc();
});

describe('per-device settings', () => {
  it('round-trips and clears', () => {
    expect(loadOverrides()).toBeUndefined();
    expect(saveOverrides({ intro: 'fullGame' })).toBe(true);
    expect(loadOverrides()).toEqual({ intro: 'fullGame' });
    clearOverrides();
    expect(loadOverrides()).toBeUndefined();
  });
  it('saving an empty diff clears', () => {
    saveOverrides({ hints: false });
    saveOverrides({});
    expect(loadOverrides()).toBeUndefined();
  });
  it('falls back to a cookie when localStorage is blocked', () => {
    g.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); } };
    expect(storageWorks()).toBe(true);
    expect(saveOverrides({ afterAnswer: 'grade' })).toBe(true);
    expect(loadOverrides()).toEqual({ afterAnswer: 'grade' });
    clearOverrides();
    expect(loadOverrides()).toBeUndefined();
  });
  it('reports when nothing can store settings', () => {
    g.localStorage = undefined;
    g.document = { get cookie() { return ''; }, set cookie(_s: string) {} };
    expect(storageWorks()).toBe(false);
    expect(saveOverrides({ hints: false })).toBe(false);
  });
});

import { beforeEach, describe, expect, it } from 'vitest';
import { cardKey, clearResult, loadResult, saveResult } from '../src/state';

class MemStorage {
  m = new Map<string, string>();
  getItem(k: string) { return this.m.has(k) ? this.m.get(k)! : null; }
  setItem(k: string, v: string) { this.m.set(k, v); }
  removeItem(k: string) { this.m.delete(k); }
}

const g = globalThis as any;
beforeEach(() => {
  g.window = {};
  g.sessionStorage = new MemStorage();
});

describe('state', () => {
  it('keys differ per card and per PGN', () => {
    expect(cardKey('pgn', 1)).not.toBe(cardKey('pgn', 2));
    expect(cardKey('pgn', 1)).not.toBe(cardKey('pgx', 1));
    expect(cardKey('pgn', 1)).toBe(cardKey('pgn', 1));
  });
  it('round-trips through window', () => {
    saveResult({ key: 'k', status: 'solved', played: ['g0.1'] });
    expect(loadResult('k')).toEqual({ key: 'k', status: 'solved', played: ['g0.1'] });
  });
  it('falls back to sessionStorage when the page was rebuilt', () => {
    saveResult({ key: 'k', status: 'failed', played: [], reason: 'unknown', failSan: 'a6' });
    g.window = {};
    expect(loadResult('k')!.failSan).toBe('a6');
  });
  it('clears results and survives blocked storage', () => {
    saveResult({ key: 'k', status: 'solved', played: [] });
    clearResult('k');
    expect(loadResult('k')).toBeUndefined();
    g.sessionStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() {} };
    g.window = {};
    expect(() => saveResult({ key: 'x', status: 'solved', played: [] })).not.toThrow();
    expect(loadResult('x')!.status).toBe('solved');
  });
});

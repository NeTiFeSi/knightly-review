import { describe, expect, it } from 'vitest';
import { DEFAULT_CONFIG, readConfig } from '../src/cardRef';

describe('readConfig', () => {
  it('defaults: last-move intro, flip after answering, hints on', () => {
    const c = readConfig({});
    expect(c).toEqual(DEFAULT_CONFIG);
    expect(c.intro).toBe('lastMove');
    expect(c.afterAnswer).toBe('flip');
    expect(c.hints).toBe(true);
  });
  it('accepts valid overrides and ignores invalid ones', () => {
    const c = readConfig({ intro: 'fullGame', afterAnswer: 'grade', acceptNags: [1, 3], introMoveDelay: 500 } as never);
    expect(c.intro).toBe('fullGame');
    expect(c.afterAnswer).toBe('grade');
    expect(c.acceptNags).toEqual([1, 3]);
    expect(c.introMoveDelay).toBe(500);
    const bad = readConfig({ intro: 'all', afterAnswer: 7, hints: 'yes', acceptNags: ['!'] } as never);
    expect(bad.intro).toBe('lastMove');
    expect(bad.afterAnswer).toBe('flip');
    expect(bad.hints).toBe(true);
    expect(bad.acceptNags).toEqual(DEFAULT_CONFIG.acceptNags);
  });
});

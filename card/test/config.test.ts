import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { checkConfig, configDiff, DEFAULT_CONFIG, formatConfig, readConfig } from '../src/cardRef';

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

describe('timed puzzle settings', () => {
  it('are off by default', () => {
    expect(DEFAULT_CONFIG).toMatchObject({ timed: false, timeLimit: 30, timeAffects: 'nothing', gradeSlow: 2, showClock: true });
  });
  it('drops invalid values with a warning', () => {
    const r = checkConfig({ timed: 'yes', timeLimit: 2, timeAffects: 'slow', gradeSlow: 9, showClock: false });
    expect(r.config.timed).toBe(false);
    expect(r.config.timeLimit).toBe(30);
    expect(r.config.timeAffects).toBe('nothing');
    expect(r.config.gradeSlow).toBe(2);
    expect(r.config.showClock).toBe(false);
    expect(r.warnings).toHaveLength(4);
    expect(checkConfig({ timeLimit: 45.5 }).warnings[0]).toMatch(/whole number/);
    expect(checkConfig({ timed: true, timeLimit: 45, timeAffects: 'fail' }).warnings).toEqual([]);
  });
});

describe('checkConfig', () => {
  it('warns about bad values, wrong types and unknown keys', () => {
    const r = checkConfig({ intro: 'fullgame', hints: 'yes', intr: 'x', gradeSolved: 5, flipDelay: 800 });
    expect(r.config.intro).toBe('lastMove');
    expect(r.config.hints).toBe(true);
    expect(r.config.gradeSolved).toBe(3);
    expect(r.config.flipDelay).toBe(800);
    expect(r.valid).toEqual({ flipDelay: 800 });
    expect(r.warnings).toHaveLength(4);
    expect(r.warnings.join('\n')).toMatch(/intro = 'fullgame' is not valid/);
    expect(r.warnings.join('\n')).toMatch(/unknown setting "intr"/);
  });
  it('treats null (template script failed) as unreadable, undefined as no config', () => {
    expect(checkConfig(null).warnings[0]).toMatch(/could not be read/);
    expect(checkConfig(undefined)).toEqual({ config: DEFAULT_CONFIG, valid: {}, warnings: [] });
  });
  it('layers device overrides on the template', () => {
    const tpl = checkConfig({ intro: 'fullGame', hints: false }).config;
    const dev = checkConfig({ hints: true, afterAnswer: 'grade' }, tpl, 'Device settings').config;
    expect(dev.intro).toBe('fullGame');
    expect(dev.hints).toBe(true);
    expect(dev.afterAnswer).toBe('grade');
    expect(configDiff(dev, tpl)).toEqual({ hints: true, afterAnswer: 'grade' });
    expect(configDiff({ ...tpl, acceptNags: [1, 3, 5, 8] }, tpl)).toEqual({});
  });
  it('formats a config block the template and the add-on agree on', () => {
    const text = formatConfig({ ...DEFAULT_CONFIG, intro: 'fullGame' });
    expect(text).toMatch(/^window\.KR_CONFIG = \{\n  acceptNags: \[1, 3, 5, 8\],/);
    expect(text).toContain("  intro: 'fullGame',");
    expect(text).toMatch(/showClock: true\n\};$/);
    const loader = readFileSync(new URL('../templates/loader.html', import.meta.url), 'utf8');
    expect(loader).toContain(formatConfig(DEFAULT_CONFIG));
  });
});

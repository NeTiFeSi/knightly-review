import { describe, expect, it } from 'vitest';
import { pickEase } from '../src/app';
import { DEFAULT_CONFIG } from '../src/cardRef';

const timed = { ...DEFAULT_CONFIG, timed: true, timeAffects: 'grade' as const, gradeSlow: 2 };

describe('pickEase', () => {
  it('untimed: failed, hinted, solved', () => {
    expect(pickEase({ status: 'failed' }, DEFAULT_CONFIG)).toEqual({ ease: 1, slow: false });
    expect(pickEase({ status: 'solved', hints: 1 }, DEFAULT_CONFIG)).toEqual({ ease: 2, slow: false });
    expect(pickEase({ status: 'solved' }, DEFAULT_CONFIG)).toEqual({ ease: 3, slow: false });
  });
  it('a slow solve is graded gradeSlow only with timeAffects grade', () => {
    expect(pickEase({ status: 'solved', timedOut: true }, timed)).toEqual({ ease: 2, slow: true });
    expect(pickEase({ status: 'solved', timedOut: false }, timed)).toEqual({ ease: 3, slow: false });
    expect(pickEase({ status: 'solved', timedOut: true }, { ...timed, timeAffects: 'nothing' })).toEqual({ ease: 3, slow: false });
    expect(pickEase({ status: 'solved', timedOut: true }, { ...timed, timed: false })).toEqual({ ease: 3, slow: false });
  });
  it('never grades better than either the slow or the hint ease', () => {
    expect(pickEase({ status: 'solved', hints: 1, timedOut: true }, { ...timed, gradeSlow: 3 })).toEqual({ ease: 2, slow: false });
    expect(pickEase({ status: 'solved', hints: 1, timedOut: true }, { ...timed, gradeSlow: 1 })).toEqual({ ease: 1, slow: true });
  });
  it('a timeout failure is graded as failed', () => {
    expect(pickEase({ status: 'failed', timedOut: true }, timed)).toEqual({ ease: 1, slow: false });
  });
});

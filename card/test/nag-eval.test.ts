import { describe, expect, it } from 'vitest';
import { cleanComment, evalToCp, formatEval, whiteShare } from '../src/eval';
import { DEFAULT_ACCEPT, isAccepted, isRejected, moveNag, nagInfo } from '../src/nag';
import { parseGames } from '../src/pgn';

describe('nag', () => {
  it('accepts !!, ! and !? by default', () => {
    for (const n of [1, 3, 5, 8]) expect(isAccepted([n], DEFAULT_ACCEPT)).toBe(true);
    for (const n of [2, 4, 6]) expect(isAccepted([n], DEFAULT_ACCEPT)).toBe(false);
  });
  it('rejects ?!, ? and ??', () => {
    for (const n of [2, 4, 6]) expect(isRejected([n], DEFAULT_ACCEPT)).toBe(true);
    expect(isRejected([], DEFAULT_ACCEPT)).toBe(false);
    expect(isRejected([14], DEFAULT_ACCEPT)).toBe(false); // positional NAG, not a move grade
  });
  it('honours a custom accept list', () => {
    expect(isAccepted([5], [1, 3])).toBe(false);
    expect(isRejected([5], [1, 3])).toBe(true);
  });
  it('reads glyph and $n forms through the PGN parser', () => {
    const [g] = parseGames('1. e4!? (1. d4 $3) (1. c4 $6) *');
    expect(g.root.children.map(c => c.nags)).toEqual([[5], [3], [6]]);
    expect(nagInfo(g.root.children[2].nags)!.label).toBe('Dubious');
    expect(moveNag([14, 1])).toBe(1);
  });
});

describe('eval', () => {
  it('formats pawns and mates', () => {
    expect(formatEval({ pawns: 0.3 })).toBe('+0.3');
    expect(formatEval({ pawns: -1.45 })).toBe('−1.4');
    expect(formatEval({ pawns: 0 })).toBe('0.0');
    expect(formatEval({ pawns: 12.3 })).toBe('+12');
    expect(formatEval({ mate: 3 })).toBe('#3');
    expect(formatEval({ mate: -2 })).toBe('#-2');
  });
  it('orders mates above any centipawn score', () => {
    expect(evalToCp({ mate: 5 })).toBeGreaterThan(evalToCp({ pawns: 50 }));
    expect(evalToCp({ mate: 1 })).toBeGreaterThan(evalToCp({ mate: 5 }));
    expect(evalToCp({ mate: -1 })).toBeLessThan(evalToCp({ mate: -5 }));
  });
  it('maps to a bar share', () => {
    expect(whiteShare({ pawns: 0 })).toBeCloseTo(0.5);
    expect(whiteShare({ pawns: 3 })).toBeGreaterThan(0.7);
    expect(whiteShare({ mate: -1 })).toBe(0);
  });
  it('parses [%eval] with depth and strips commands from comments', () => {
    const [g] = parseGames('1. e4 { [%eval 0.17,22] [%kr 1] [%clk 0:01:00] Good move } (1. d4 { [%eval #-3] }) *');
    const [e4, d4] = g.root.children;
    expect(e4.eval).toEqual({ pawns: 0.17, depth: 22 });
    expect(e4.comment).toBe('Good move');
    expect(e4.puzzleId).toBe(1);
    expect(d4.eval).toEqual({ mate: -3 });
    expect(cleanComment('[%cal Ge2e4] hi  there')).toBe('hi there');
  });
  it('leaves eval undefined when missing', () => {
    const [g] = parseGames('1. e4 *');
    expect(g.root.children[0].eval).toBeUndefined();
  });
});

import { acceptList, isAccepted as accepted } from '../src/nag';

describe('moves with no NAG (normal play)', () => {
  it('are rejected by default and accepted with acceptNeutral', () => {
    const base = { acceptNags: [1, 3, 5, 8], acceptNeutral: false };
    expect(accepted([], acceptList(base))).toBe(false);
    expect(accepted([], acceptList({ ...base, acceptNeutral: true }))).toBe(true);
    expect(accepted([6], acceptList({ ...base, acceptNeutral: true }))).toBe(false);
  });
});

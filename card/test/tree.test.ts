// The solution tree: alias leaves (transpositions), choosing a reply, and the draw rules.
import { describe, expect, it } from 'vitest';
import { expandAlias, fenKey, findNode, findPuzzle, parseGames } from '../src/pgn';
import { DEFAULT_ACCEPT } from '../src/nag';
import { pickReply, PuzzleRun, Visits } from '../src/puzzle';
import { mv } from './helpers';

const start = (pgn: string, visits?: Visits, random: () => number = () => 0) => {
  const games = parseGames(pgn);
  return { games, run: new PuzzleRun(findPuzzle(games, 1)!.position, DEFAULT_ACCEPT, undefined, false, visits, random) };
};

describe('transpositions', () => {
  // 1. e4 e5 2. d4 and 1. d4 e5 2. e4 reach the same position; only the first is written on.
  const PGN = `1. a3 $4 { [%kr 1] } ( 1. e4 $1 e5 2. d4 $1 exd4 ) ( 1. d4 $1 e5 2. e4 $1 ) *`;

  it('marks a leaf that is written in full elsewhere as its alias', () => {
    const games = parseGames(PGN);
    const leaf = findNode(games, 'g0.2.0.0')!; // 1. d4 e5 2. e4
    expect(leaf.san).toBe('e4');
    expect(leaf.children).toHaveLength(0);
    expect(leaf.alias?.san).toBe('d4');
    expandAlias(leaf);
    expect(leaf.children.map(c => c.san)).toEqual(['exd4']);
    expect(leaf.children[0].id).toBe(leaf.id + '.0');
    expect(findNode(games, leaf.id + '.0')).toBe(leaf.children[0]); // ids of the copies can be found again
  });

  it('plays on through the alias, whichever move order reached it', () => {
    const { run } = start(PGN);
    expect(run.play(mv(run.cur, 'd2d4')).kind).toBe('continue'); // e5 is the reply
    const out = run.play(mv(run.cur, 'e2e4'));
    expect(out.kind).toBe('solved'); // the reply exd4 is a leaf
    if (out.kind === 'solved') expect(out.reply!.san).toBe('exd4');
  });

  it('finds the position by fen key, not by the move counters', () => {
    const games = parseGames(PGN);
    const a = findNode(games, 'g0.2.0.0')!; // 1. d4 e5 2. e4
    const b = findNode(games, 'g0.1.0.0')!; // 1. e4 e5 2. d4
    expect(fenKey(a.fen)).toBe(fenKey(b.fen));
  });
});

describe('replies', () => {
  const PGN = `1. a3 $4 { [%kr 1] } ( 1. e4 $1 e5 ( 1... c5 ) ( 1... e6 ) ) *`;
  const replies = () => parseGames(PGN)[0].root.children[1].children; // the replies to 1. e4

  it('prefers the reply seen least, and chooses among equals at random', () => {
    const r = replies();
    expect(r.map(n => n.san)).toEqual(['e5', 'c5', 'e6']);
    const counts: Record<string, number> = {};
    const visits: Visits = { get: k => counts[k] || 0, add: k => (counts[k] = (counts[k] || 0) + 1) };
    expect(pickReply(r, visits, () => 0)?.san).toBe('e5');
    visits.add(fenKey(r[0].fen));
    expect(pickReply(r, visits, () => 0)?.san).toBe('c5');
    expect(pickReply(r, visits, () => 0.99)?.san).toBe('e6');
    visits.add(fenKey(r[1].fen));
    visits.add(fenKey(r[2].fen));
    expect(pickReply(r, visits, () => 0)?.san).toBe('e5'); // all seen once again: a tie
    expect(pickReply([], visits)).toBeUndefined();
  });

  it('counts the shown reply', () => {
    const counts: Record<string, number> = {};
    const visits: Visits = { get: k => counts[k] || 0, add: k => (counts[k] = (counts[k] || 0) + 1) };
    const { run } = start(PGN, visits);
    run.play(mv(run.cur, 'e2e4'));
    expect(Object.values(counts)).toEqual([1]);
  });
});

describe('draws', () => {
  const PGN = `1. Nf3 $1 { [%kr 1] } Nf6 2. Ng1 $1 Ng8 3. Nf3 $1 Nf6 4. Ng1 $1 Ng8 5. e4 *`;

  it('ends the line as solved when a position comes up a third time', () => {
    const { run } = start(PGN);
    expect(run.play(mv(run.cur, 'g1f3')).kind).toBe('continue');
    expect(run.play(mv(run.cur, 'f3g1')).kind).toBe('continue');
    expect(run.play(mv(run.cur, 'g1f3')).kind).toBe('continue');
    const out = run.play(mv(run.cur, 'f3g1'));
    expect(out.kind).toBe('solved');
    if (out.kind === 'solved') expect(out.draw).toBe('Draw by threefold repetition.');
    expect(run.draw).toBe('Draw by threefold repetition.');
  });

  it('ends the line when the fifty-move clock runs out', () => {
    const pgn = `[FEN "4k3/8/8/8/8/8/4P3/4K1N1 w - - 98 80"]\n[SetUp "1"]\n\n1. Nf3 $1 { [%kr 1] } Kd7 2. Ng1 $1 Ke7 3. Nf3 *`;
    const { run } = start(pgn);
    const out = run.play(mv(run.cur, 'g1f3'));
    expect(out.kind).toBe('solved'); // the clock reads 99 after the move and 100 after Black's reply
    if (out.kind === 'solved') expect(out.draw).toBe('Draw by the fifty-move rule.');
  });
});

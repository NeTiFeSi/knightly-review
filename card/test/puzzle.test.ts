import { describe, expect, it } from 'vitest';
import { findPuzzle, parseGames } from '../src/pgn';
import { acceptedMoves, mateMiss, PuzzleRun } from '../src/puzzle';
import { DEFAULT_ACCEPT } from '../src/nag';
import { games, mv } from './helpers';

const run = (n: number, accept = DEFAULT_ACCEPT) => new PuzzleRun(findPuzzle(games(), n)!.position, accept);

describe('PuzzleRun', () => {
  it('accepts a good variation and auto-plays the reply (black to move)', () => {
    const r = run(1);
    expect(r.userColor).toBe('black');
    const out = r.play(mv(r.cur, 'd6e5'));
    expect(out.kind).toBe('solved');
    if (out.kind !== 'solved') return;
    expect(out.played.san).toBe('dxe5');
    expect(out.reply!.san).toBe('Qxg4');
    expect(r.status).toBe('solved');
  });

  it('rejects the mainline blunder', () => {
    const r = run(1);
    const out = r.play(mv(r.cur, 'g4d1'));
    expect(out).toMatchObject({ kind: 'failed', reason: 'rejected', san: 'Bxd1' });
    expect(r.status).toBe('failed');
  });

  it('rejects a dubious variation and reports its grade node', () => {
    const r = run(1);
    const out = r.play(mv(r.cur, 'g4e6'));
    expect(out.kind).toBe('failed');
    if (out.kind === 'failed') expect(out.node!.nags).toEqual([6]);
  });

  it('fails a legal move that is not in the analysis', () => {
    const r = run(1);
    const out = r.play(mv(r.cur, 'a7a6'));
    expect(out).toMatchObject({ kind: 'failed', reason: 'unknown', san: 'a6' });
  });

  it('continues a multi-move line and accepts the line move or a good sibling', () => {
    const a = run(3);
    expect(a.play(mv(a.cur, 'g7g6')).kind).toBe('continue');
    expect(a.cur.san).toBe('Qf3');
    expect(a.accepted().map(n => n.san)).toEqual(['Nf6', 'Qf6']);
    expect(a.play(mv(a.cur, 'g8f6'))).toMatchObject({ kind: 'solved' });

    const b = run(3);
    b.play(mv(b.cur, 'g7g6'));
    expect(b.play(mv(b.cur, 'd8f6')).kind).toBe('solved');

    const c = run(3);
    c.play(mv(c.cur, 'g7g6'));
    expect(c.play(mv(c.cur, 'd8e7'))).toMatchObject({ kind: 'failed', reason: 'unknown' });
    expect(c.played.map(n => n.san)).toEqual(['g6']);
  });

  it('does not count castling as a capture for the fifty-move rule', () => {
    const pgn = '[FEN "4k3/p7/8/8/8/8/P7/4K2R w K - 99 80"]\n[SetUp "1"]\n\n1. Kd1 $2 { [%kr 1] } (1. O-O $1) *';
    const r = new PuzzleRun(findPuzzle(parseGames(pgn), 1)!.position, DEFAULT_ACCEPT);
    expect(r.play(mv(r.cur, 'e1g1'))).toMatchObject({ kind: 'solved', draw: 'Draw by the fifty-move rule.' });
  });

  it('handles castling dragged as king two squares (white to move)', () => {
    const r = run(4);
    expect(r.userColor).toBe('white');
    expect(r.play(mv(r.cur, 'f3d4')).kind).toBe('continue');
    expect(r.play(mv(r.cur, 'e1g1'))).toMatchObject({ kind: 'solved' });
    expect(r.played.map(n => n.san)).toEqual(['Nxd4', 'O-O']);
  });

  it('also accepts castling as king-takes-rook', () => {
    const r = run(4);
    expect(r.play(mv(r.cur, 'e1h1')).kind).toBe('solved'); // 4. O-O!? has no continuation
  });

  it('first move is graded by NAG only', () => {
    const p = findPuzzle(games(), 1)!;
    expect(acceptedMoves(p.position, true, DEFAULT_ACCEPT).map(n => n.san)).toEqual(['dxe5']);
    expect(acceptedMoves(p.position, true, [1, 3, 5, 6]).map(n => n.san)).toEqual(['dxe5', 'Be6']);
  });

  it('respects a stricter accept list', () => {
    const r = run(3, [1, 3]);
    r.play(mv(r.cur, 'g7g6'));
    expect(r.play(mv(r.cur, 'd8f6'))).toMatchObject({ kind: 'failed', reason: 'rejected' });
  });

  it('handles promotion', () => {
    const [g] = parseGames('[SetUp "1"] [FEN "4k3/P7/8/8/8/8/8/K7 w - - 0 1"] 1. Kb2?? { [%kr 1] } (1. a8=Q+! Kd7 2. Qb7+) (1. a8=N?!) *');
    const r = new PuzzleRun(findPuzzle([g], 1)!.position, DEFAULT_ACCEPT);
    expect(r.play(mv(r.cur, 'a7a8q')).kind).toBe('continue');
    expect(r.play(mv(r.cur, 'a8b7')).kind).toBe('solved');
    const u = new PuzzleRun(findPuzzle([g], 1)!.position, DEFAULT_ACCEPT);
    expect(u.play(mv(u.cur, 'a7a8n'))).toMatchObject({ kind: 'failed', reason: 'rejected' });
  });

  it('give up fails at the current position', () => {
    const r = run(2);
    expect(r.giveUp()).toMatchObject({ kind: 'failed', reason: 'gaveup' });
    expect(() => r.play(mv(r.cur, 'g8f6'))).toThrow();
  });
});

describe('timeout', () => {
  it('fails at the current position with reason timeout', () => {
    const r = run(2);
    expect(r.timeout()).toMatchObject({ kind: 'failed', reason: 'timeout' });
    expect(r.status).toBe('failed');
    expect(() => r.play(mv(r.cur, 'g8f6'))).toThrow();
  });
});

describe('hints', () => {
  it('reveals the text hint first, then the piece to move', () => {
    const p = findPuzzle(games(), 1)!;
    expect(p.gameMove.hint).toBe('Which of your pieces can still take back?');
    const r = new PuzzleRun(p.position, DEFAULT_ACCEPT, p.gameMove.hint);
    expect(r.hint()).toEqual({ kind: 'text', text: 'Which of your pieces can still take back?' });
    const piece = r.hint();
    expect(piece && piece.kind).toBe('piece');
    expect(r.hasMoreHints()).toBe(false);
    expect(r.hint()).toBeUndefined();
    expect(r.hintsUsed).toBe(2);
  });
  it('without text, offers the piece; hints reset as the line advances', () => {
    const r = run(3);
    const h = r.hint()!;
    expect(h.kind).toBe('piece');
    if (h.kind === 'piece') expect(h.squares.length).toBe(2); // g7 (g6) and d8 (Qe7, Qf6)
    r.play(mv(r.cur, 'g7g6'));
    expect(r.hints).toEqual([]);
    expect(r.hasMoreHints()).toBe(true);
    r.hint();
    expect(r.hintsUsed).toBe(2);
  });
});

describe('forced mates', () => {
  // Ra8 mates at once; "Ra7" is scripted as a slower forced mate; Kf1 is won but no mate.
  const PGN = `[FEN "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"]
[SetUp "1"]

1. h3 $4 { [%eval 4.5] [%kr 1] } ( 1. Ra8# $1 { [%eval #1] } ) ( 1. Ra7 $1 { [%eval #3] } ) ( 1. Kf1 $1 { [%eval 6.5] } ) 1... h6 *`;
  const mateRun = (strict = false) => new PuzzleRun(findPuzzle(parseGames(PGN), 1)!.position, DEFAULT_ACCEPT, undefined, strict);

  it('accepts any move that keeps a forced mate, and nothing else', () => {
    expect(mateRun().accepted().map(n => n.san)).toEqual(['Ra8#', 'Ra7']);
    expect(mateRun().play(mv(mateRun().cur, 'a1a7')).kind).toBe('solved');
    const r = mateRun();
    expect(r.play(mv(r.cur, 'g1f1'))).toMatchObject({ kind: 'failed', reason: 'rejected', san: 'Kf1' });
  });

  it('accepts only the fastest mate when strict', () => {
    expect(mateRun(true).accepted().map(n => n.san)).toEqual(['Ra8#']);
    const pos = findPuzzle(parseGames(PGN), 1)!.position;
    expect(acceptedMoves(pos, true, DEFAULT_ACCEPT, true).map(n => n.san)).toEqual(['Ra8#']);
  });

  it('says why a move fell short of the mate', () => {
    const pos = findPuzzle(parseGames(PGN), 1)!.position;
    const child = (san: string) => pos.children.find(c => c.san === san)!;
    expect(mateMiss(pos, child('Kf1'), false)).toEqual({ kind: 'drops', best: 1 });
    expect(mateMiss(pos, child('Ra7'), false)).toBeUndefined();
    expect(mateMiss(pos, child('Ra7'), true)).toEqual({ kind: 'slower', mate: 3, best: 1 });
    expect(mateMiss(pos, child('Ra8#'), true)).toBeUndefined();
  });

  it("ignores the opponent's mates (Black to move, White's evals)", () => {
    const pgn = `[FEN "r5k1/5ppp/8/8/8/8/5PPP/6K1 b - - 0 1"]
[SetUp "1"]

1... h6 $4 { [%eval -4.5] [%kr 1] } ( 1... Ra1# $1 { [%eval #-1] } ) ( 1... Ra2 $1 { [%eval #-3] } ) ( 1... Kf8 $1 { [%eval -6.5] } ) *`;
    const pos = findPuzzle(parseGames(pgn), 1)!.position;
    expect(acceptedMoves(pos, true, DEFAULT_ACCEPT).map(n => n.san)).toEqual(['Ra1#', 'Ra2']);
    expect(acceptedMoves(pos, true, DEFAULT_ACCEPT, true).map(n => n.san)).toEqual(['Ra1#']);
  });
});

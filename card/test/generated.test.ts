// Contract test: a note written by the Python generator (core/knightly/notes.py) must
// work as a puzzle here. The fixture is the core's golden file; regenerate it with
// `UPDATE_GOLDEN=1 python -m unittest discover -s tests` in core/.
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { findPuzzle, parseGames } from '../src/pgn';
import { PuzzleRun } from '../src/puzzle';
import { DEFAULT_ACCEPT } from '../src/nag';
import { mv } from './helpers';

const PGN = readFileSync(new URL('../../core/tests/fixtures/generated_note.pgn', import.meta.url), 'utf8');
const run = () => new PuzzleRun(findPuzzle(parseGames(PGN), 1)!.position, DEFAULT_ACCEPT);

describe('generated note', () => {
  it('parses, with puzzle 1 at the tagged move', () => {
    const gs = parseGames(PGN);
    expect(gs).toHaveLength(1);
    expect(findPuzzle(gs, 1)).toBeDefined();
    expect(findPuzzle(gs, 2)).toBeUndefined();
  });

  it('accepts the best move and plays the reply', () => {
    const r = run();
    expect(r.userColor).toBe('black');
    const out = r.play(mv(r.cur, 'g7g6'));
    expect(out.kind).toBe('solved');
    if (out.kind === 'solved') expect(out.reply!.san).toBe('Qf3');
  });

  it('accepts the other good alternatives', () => {
    for (const uci of ['d8e7', 'd8f6']) {
      const r = run();
      expect(r.play(mv(r.cur, uci)).kind).toBe('solved');
    }
  });

  it('rejects the move that was played in the game', () => {
    const r = run();
    expect(r.play(mv(r.cur, 'g8f6'))).toMatchObject({ kind: 'failed', reason: 'rejected', san: 'Nf6' });
  });
});

// An opening+ply note: one game per card, several games in one PGN field.
const OPENING = readFileSync(new URL('../../core/tests/fixtures/generated_opening_note.pgn', import.meta.url), 'utf8');

describe('generated opening note', () => {
  it('finds each card in its own game', () => {
    const gs = parseGames(OPENING);
    expect(gs).toHaveLength(2);
    expect(findPuzzle(gs, 1)).toBeDefined();
    expect(findPuzzle(gs, 2)).toBeDefined();
    expect(findPuzzle(gs, 3)).toBeUndefined();
  });

  it('plays card 2 from the second game', () => {
    const r = new PuzzleRun(findPuzzle(parseGames(OPENING), 2)!.position, DEFAULT_ACCEPT);
    expect(r.userColor).toBe('black');
    expect(r.play(mv(r.cur, 'f8b4')).kind).toBe('solved');
    const again = new PuzzleRun(findPuzzle(parseGames(OPENING), 2)!.position, DEFAULT_ACCEPT);
    expect(again.play(mv(again.cur, 'g8f6'))).toMatchObject({ kind: 'failed', reason: 'rejected' });
  });
});

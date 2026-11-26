// Contract test: a note written by the Python generator (core/knightly/notes.py) must
// work as a puzzle here (both of its puzzles). The fixture is the core's golden file; regenerate it with
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
    expect(findPuzzle(gs, 2)).toBeDefined();
    expect(findPuzzle(gs, 3)).toBeUndefined();
  });

  it('puzzle 2 (the punishment) is found in the same game and is solved by Qxf7#', () => {
    const r = new PuzzleRun(findPuzzle(parseGames(PGN), 2)!.position, DEFAULT_ACCEPT);
    expect(r.userColor).toBe('white');
    expect(r.play(mv(r.cur, 'h5f7')).kind).toBe('solved');
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

// The punishment is not the game's own reply: it sits in a short game of its own.
const SHORT = readFileSync(new URL('../../core/tests/fixtures/generated_note_short.pgn', import.meta.url), 'utf8');

describe('generated note with the punishment in its own game', () => {
  it('finds each puzzle in its own game', () => {
    const gs = parseGames(SHORT);
    expect(gs).toHaveLength(2);
    expect(findPuzzle(gs, 1)).toBeDefined();
    expect(findPuzzle(gs, 2)).toBeDefined();
    expect(findPuzzle(gs, 3)).toBeUndefined();
  });

  it('plays puzzle 2 as the punisher', () => {
    const r = new PuzzleRun(findPuzzle(parseGames(SHORT), 2)!.position, DEFAULT_ACCEPT);
    expect(r.userColor).toBe('white');
    expect(r.play(mv(r.cur, 'h5f7')).kind).toBe('solved');
  });
});

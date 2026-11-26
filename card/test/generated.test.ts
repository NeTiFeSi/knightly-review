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
    if (out.kind === 'solved') expect(out.reply).toBeUndefined(); // the solution tree ends after the move
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

// A note made from a position inside a mistake's tree: it starts from a FEN, with the opponent's last move.
const SUB = readFileSync(new URL('../../core/tests/fixtures/generated_sub_note.pgn', import.meta.url), 'utf8');

describe('generated sub-note', () => {
  it('finds both cards, the position after the lead-in move', () => {
    const gs = parseGames(SUB);
    expect(findPuzzle(gs, 1)!.position.san).toBe('Nc3');
    expect(findPuzzle(gs, 2)).toBeDefined();
  });

  it('accepts a good move with the opponent reply, and rejects the mistake', () => {
    const r = new PuzzleRun(findPuzzle(parseGames(SUB), 1)!.position, DEFAULT_ACCEPT);
    expect(r.userColor).toBe('black');
    expect(r.play(mv(r.cur, 'g7g6')).kind).toBe('solved');
    const w = new PuzzleRun(findPuzzle(parseGames(SUB), 1)!.position, DEFAULT_ACCEPT);
    expect(w.play(mv(w.cur, 'b7b6'))).toMatchObject({ kind: 'failed', reason: 'rejected' });
  });

  it('plays the punishment as one move and a reply', () => {
    const r = new PuzzleRun(findPuzzle(parseGames(SUB), 2)!.position, DEFAULT_ACCEPT);
    expect(r.userColor).toBe('white');
    const out = r.play(mv(r.cur, 'h5f7'));
    expect(out.kind).toBe('solved');
    if (out.kind === 'solved') expect(out.reply!.san).toBe('Qxf7');
  });
});

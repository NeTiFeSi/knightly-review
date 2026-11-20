import { Chess, normalizeMove } from 'chessops/chess';
import { makeFen, parseFen } from 'chessops/fen';
import { parseUci } from 'chessops/util';
import { describe, expect, it } from 'vitest';
import { ELine, expectation, LiveEngine } from '../src/engine';
import { LiveLine } from '../src/live';
import { findPuzzle } from '../src/pgn';
import { engineUci, PuzzleRun } from '../src/puzzle';
import { games, mv } from './helpers';

const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const AFTER_E4_E5 = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2';

function play(fen: string, uci: string): string {
  const pos = Chess.fromSetup(parseFen(fen).unwrap()).unwrap();
  pos.play(normalizeMove(pos, parseUci(uci)!));
  return makeFen(pos.toSetup());
}

const line = (uci: string, wdl: [number, number, number], pv: string[] = [uci]): ELine => ({ uci, pv, wdl });

/** Answers from a table keyed by the FEN's first four fields (+ the searched moves). */
class ScriptedEngine implements LiveEngine {
  readonly name = 'Scripted';
  calls: string[] = [];
  constructor(private readonly table: Record<string, ELine[]>) {}
  analyse(fen: string, multipv: number, moves?: string[]): Promise<ELine[]> {
    const key = fen.split(' ').slice(0, 4).join(' ') + (moves && moves.length ? ' | ' + moves.join(',') : '');
    this.calls.push(key);
    const lines = this.table[key];
    if (!lines) return Promise.reject(new Error('no script for ' + key));
    return Promise.resolve(lines.slice(0, multipv));
  }
}

const k = (fen: string) => fen.split(' ').slice(0, 4).join(' ');
const OPTS = { goodMove: 2, maxMoves: 10 };

describe('expectation', () => {
  it('is wins + draws / 2, from either side', () => {
    const l = line('e2e4', [73, 923, 4]);
    expect(expectation(l, true)).toBeCloseTo(53.45);
    expect(expectation(l, false)).toBeCloseTo(46.55);
  });
});

describe('LiveLine', () => {
  const script = (): Record<string, ELine[]> => ({
    // Only e4 is good here (d4 loses 50 %).
    [k(START)]: [line('e2e4', [500, 500, 0]), line('d2d4', [0, 500, 500])],
    [k(START) + ' | g1f3']: [line('g1f3', [0, 1000, 0])],
    [k('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1')]: [line('e7e5', [500, 500, 0])],
    // Two good moves after 1. e4 e5: quiet.
    [k(AFTER_E4_E5)]: [line('g1f3', [300, 600, 100]), line('b1c3', [300, 580, 120])],
  });

  it('plays on while forced, then stops at a quiet position', async () => {
    const eng = new ScriptedEngine(script());
    const live = new LiveLine(eng, START, 'white', OPTS, false);
    expect(await live.endHere()).toBeUndefined();
    expect(live.best()).toBe('e2e4');
    const step = await live.play('e2e4');
    expect(step).toMatchObject({ kind: 'solved', move: 'e2e4', reply: 'e7e5', reason: 'quiet' });
  });

  it('fails a move that loses more than the good-move margin', async () => {
    const live = new LiveLine(new ScriptedEngine(script()), START, 'white', OPTS, false);
    const step = await live.play('d2d4');
    expect(step).toMatchObject({ kind: 'failed', san: 'd4', best: 'e2e4', bestSan: 'e4' });
    if (step.kind === 'failed') expect(step.loss).toBeCloseTo(50);
  });

  it('asks the engine about a move outside its top lines', async () => {
    const eng = new ScriptedEngine(script());
    const live = new LiveLine(eng, START, 'white', OPTS, false);
    const step = await live.play('g1f3'); // 50 % vs e4's 75 %
    expect(step.kind).toBe('failed');
    expect(eng.calls).toContain(k(START) + ' | g1f3');
  });

  it('stops at the move limit', async () => {
    const live = new LiveLine(new ScriptedEngine(script()), START, 'white', { goodMove: 2, maxMoves: 1 }, false);
    const t = script();
    // Make the position after 1. e4 e5 forcing, so only the limit can end it.
    t[k(AFTER_E4_E5)] = [line('g1f3', [900, 100, 0]), line('b1c3', [100, 800, 100])];
    const limited = new LiveLine(new ScriptedEngine(t), START, 'white', { goodMove: 2, maxMoves: 1 }, false);
    expect(await limited.play('e2e4')).toMatchObject({ kind: 'solved', reason: 'limit' });
    expect(live).toBeDefined();
  });

  it('ends on mate without asking for a reply', async () => {
    const fen = '7k/6pp/8/8/8/8/8/R5K1 w - - 0 1';
    const eng = new ScriptedEngine({ [k(fen)]: [line('a1a8', [1000, 0, 0]), line('a1a2', [0, 1000, 0])] });
    const live = new LiveLine(eng, fen, 'white', OPTS, false);
    expect(await live.play('a1a8')).toMatchObject({ kind: 'solved', reason: 'mate' });
    expect(eng.calls).toHaveLength(1);
  });

  it('is not quiet right after a capture, even with two good moves', async () => {
    const fen = AFTER_E4_E5;
    const eng = new ScriptedEngine({ [k(fen)]: [line('g1f3', [300, 600, 100]), line('b1c3', [300, 580, 120])] });
    expect(await new LiveLine(eng, fen, 'white', OPTS, true).endHere()).toBeUndefined();
    expect(await new LiveLine(eng, fen, 'white', OPTS, false).endHere()).toBe('quiet');
  });
});

/** FEN after the PGN line of puzzle 1 (5... dxe5 6. Qxg4), from a run without an engine. */
function afterPgnLine(): string {
  const p = findPuzzle(games(), 1)!;
  const run = new PuzzleRun(p.position, [1, 3, 5, 8]);
  run.play(mv(run.cur, 'd6e5'));
  return run.line[run.line.length - 1].fen;
}

describe('PuzzleRun with a live engine', () => {
  it('hands over to the engine where the PGN ends, plays on after a capture, and stops when quiet', async () => {
    const p = findPuzzle(games(), 1)!;
    // Puzzle 1: 5... dxe5! 6. Qxg4 ends the PGN line. Qxg4 was a capture, so play goes on.
    const afterReply = afterPgnLine();
    const mid = play(afterReply, 'g8f6');
    const next = play(mid, 'd2d3');
    const eng = new ScriptedEngine({
      [k(afterReply)]: [line('g8f6', [100, 800, 100]), line('d8d7', [100, 790, 110])],
      [k(mid)]: [line('d2d3', [100, 800, 100])],
      [k(next)]: [line('f6g4', [100, 800, 100]), line('c8d7', [100, 790, 110])],
    });
    const run = new PuzzleRun(p.position, [1, 3, 5, 8], undefined, { engine: eng, options: OPTS });
    expect(run.play(mv(run.cur, 'd6e5')).kind).toBe('live');
    expect(k(run.cur.fen)).toBe(k(afterReply));
    expect(await run.startLive()).toBe(false);
    const out = await run.playLive(mv(run.cur, 'g8f6'));
    expect(out.kind).toBe('solved');
    expect(run.liveEnd).toBe('quiet');
    expect(run.liveMoves).toEqual(['g8f6', 'd2d3']);
    expect(run.played.map(n => n.san)).toEqual(['dxe5', 'Nf6']);
  });

  it('grafts live moves onto the tree and fails a bad one', async () => {
    const p = findPuzzle(games(), 1)!;
    const afterReply = afterPgnLine();
    const eng = new ScriptedEngine({
      [k(afterReply)]: [line('g8f6', [100, 800, 100]), line('h7h5', [600, 300, 100])],
      [k(afterReply) + ' | a7a6']: [line('a7a6', [700, 250, 50])],
    });
    const run = new PuzzleRun(p.position, [1, 3, 5, 8], undefined, { engine: eng, options: OPTS });
    run.play(mv(run.cur, 'd6e5'));
    expect(await run.startLive()).toBe(false); // only one good move: keep playing
    const before = run.cur.children.length;
    const out = await run.playLive(mv(run.cur, 'a7a6'));
    expect(out).toMatchObject({ kind: 'failed', reason: 'live', san: 'a6', bestSan: 'Nf6' });
    expect(run.cur.children.length).toBe(before + 1);
    expect(run.cur.children[before].live).toBe(true);
    expect(run.liveMoves).toEqual(['a7a6']);
  });

  it('without an engine the puzzle ends where the PGN does', () => {
    const p = findPuzzle(games(), 1)!;
    const run = new PuzzleRun(p.position, [1, 3, 5, 8]);
    expect(run.play(mv(run.cur, 'd6e5')).kind).toBe('solved');
  });

  it('turns chessops castling into engine UCI', () => {
    const g = games()[2];
    // Before 5. O-O in game 3's variation: White can castle short.
    const p = findPuzzle(games(), 4)!;
    const nxd4 = p.position.children.find(c => c.san === 'Nxd4')!;
    const exd4 = nxd4.children[0];
    expect(engineUci(exd4, mv(exd4, 'e1g1'))).toBe('e1g1');
    expect(g).toBeDefined();
  });
});

describe('built-in engine output', () => {
  it('parses UCI info lines with WDL and multipv', async () => {
    const { parseInfo, toWhite } = await import('../src/wasm');
    const i = parseInfo('info depth 14 seldepth 20 multipv 2 score cp -35 wdl 40 800 160 nodes 1 nps 1 pv d7d5 e4d5 d8d5');
    expect(i).toEqual({ idx: 2, line: { uci: 'd7d5', pv: ['d7d5', 'e4d5', 'd8d5'], wdl: [40, 800, 160], cp: -35, mate: undefined } });
    expect(parseInfo('info depth 14 score cp 10 upperbound wdl 1 2 3 pv e2e4')).toBeUndefined();
    expect(parseInfo('info string NNUE evaluation using nn.nnue')).toBeUndefined();
    const m = parseInfo('info depth 5 multipv 1 score mate 2 wdl 1000 0 0 pv d1h5 g7g6')!;
    // Engine output is from the side to move: Black here, so White's view is flipped.
    expect(toWhite(m.line, false)).toMatchObject({ wdl: [0, 0, 1000], mate: -2 });
    expect(toWhite(m.line, true)).toBe(m.line);
  });
});

describe('LiveLine in a forced mate', () => {
  // Scripted: from START, "e2e4" mates in 3, "d2d4" in 5, "g1f3" wins without a mate (all WDL 100 %).
  const mate = (uci: string, m: number | undefined): ELine => ({ uci, pv: [uci], wdl: [1000, 0, 0], mate: m });
  const script = (): Record<string, ELine[]> => ({
    [k(START)]: [mate('e2e4', 3), mate('d2d4', 5)],
    [k(START) + ' | g1f3']: [mate('g1f3', undefined)],
    [k('rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1')]: [line('d7d5', [1000, 0, 0])],
    [k('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1')]: [line('e7e5', [1000, 0, 0])],
    [k(AFTER_E4_E5)]: [mate('d1h5', 2)],
    [k('rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 0 2')]: [mate('c1f4', 4), mate('g1f3', 4)],
  });

  it('is never quiet, and needs a move that keeps the mate', async () => {
    const live = new LiveLine(new ScriptedEngine(script()), START, 'white', OPTS, false);
    expect(await live.endHere()).toBeUndefined(); // two mating moves, but no quiet stop
    expect(await live.play('g1f3')).toMatchObject({ kind: 'failed', bestSan: 'e4' });
  });

  it('accepts a slower mate unless strict', async () => {
    const loose = new LiveLine(new ScriptedEngine(script()), START, 'white', OPTS, false);
    expect(await loose.play('d2d4')).toMatchObject({ kind: 'continue', reply: 'd7d5' });
    const strict = new LiveLine(new ScriptedEngine(script()), START, 'white', { ...OPTS, strictMate: true }, false);
    expect(await strict.play('d2d4')).toMatchObject({ kind: 'failed' });
    expect((await new LiveLine(new ScriptedEngine(script()), START, 'white', { ...OPTS, strictMate: true }, false).play('e2e4')).kind).not.toBe('failed');
  });
});

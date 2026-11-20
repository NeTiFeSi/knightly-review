// Playing on past the PGN with a live engine: pure logic, no DOM.
//
// At each position where the user is to move:
// - the game is over → solved;
// - "quiet" → solved: two or more good moves, no check, and the last move wasn't a capture;
// - the move limit is reached → solved.
// Otherwise the user's move must be a good move (within `goodMove` win-% of the best);
// the opponent then plays the engine's best reply.
// When the best move mates by force, a good move is one that keeps a forced mate
// (with `strictMate`, one as fast as the best), and the position is never quiet.
import { Chess, normalizeMove } from 'chessops/chess';
import { makeFen, parseFen } from 'chessops/fen';
import { makeSan } from 'chessops/san';
import type { Color } from 'chessops/types';
import { parseUci } from 'chessops/util';
import { ELine, expectation, LiveEngine } from './engine';

export interface LiveOptions {
  /** Win-% (expected score) a move may lose against the best and still count as good. */
  goodMove: number;
  /** Most user moves played live. */
  maxMoves: number;
  /** In a forced mate, only a mate as fast as the engine's best counts. */
  strictMate?: boolean;
}

/** Forced mate for the user in this many moves along `l`, or undefined. */
export function mateIn(l: ELine, white: boolean): number | undefined {
  const m = l.mate;
  if (m === undefined || m === null || m === 0) return undefined;
  return (white ? m > 0 : m < 0) ? Math.abs(m) : undefined;
}

export type EndReason = 'mate' | 'draw' | 'quiet' | 'limit';

export type LiveStep =
  | { kind: 'continue'; move: string; reply: string; replyLine: ELine }
  | { kind: 'solved'; move: string; reason: EndReason; reply?: string; replyLine?: ELine }
  | { kind: 'failed'; move: string; san: string; best: string; bestSan: string; loss: number; line: ELine };

function position(fen: string): Chess {
  return Chess.fromSetup(parseFen(fen).unwrap()).unwrap();
}

function san(fen: string, uci: string): string {
  const pos = position(fen);
  const m = parseUci(uci);
  return m ? makeSan(pos, normalizeMove(pos, m)) : uci;
}

/** Play an engine UCI move (castling as e1g1). */
function after(fen: string, uci: string): { fen: string; capture: boolean } {
  const pos = position(fen);
  const m = normalizeMove(pos, parseUci(uci)!);
  if (!pos.isLegal(m)) throw new Error('illegal engine move ' + uci);
  let capture = false;
  if ('from' in m) {
    const mover = pos.board.get(m.from);
    const target = pos.board.get(m.to);
    capture = (!!target && !!mover && target.color !== mover.color) || (!!mover && mover.role === 'pawn' && m.to === pos.epSquare);
  }
  pos.play(m);
  return { fen: makeFen(pos.toSetup()), capture };
}

function over(fen: string): EndReason | undefined {
  const pos = position(fen);
  if (pos.isCheckmate()) return 'mate';
  if (pos.isEnd()) return 'draw';
  return undefined;
}

export class LiveLine {
  /** Analysis of the current position (best first), once known. */
  lines?: ELine[];
  movesPlayed = 0;
  private pending?: Promise<ELine[]>;

  constructor(
    private readonly engine: LiveEngine,
    /** Position with the user to move. */
    public fen: string,
    readonly user: Color,
    private readonly opts: LiveOptions,
    /** Whether the move that led here was a capture (an exchange may be going on). */
    private lastCapture: boolean,
  ) {}

  /** Start analysing the current position (call as soon as it's on the board). */
  prefetch(): Promise<ELine[]> {
    if (!this.pending) this.pending = this.engine.analyse(this.fen, 2).then(l => (this.lines = l));
    return this.pending;
  }

  /** Whether the puzzle should end here (before the user moves). */
  async endHere(): Promise<EndReason | undefined> {
    const o = over(this.fen);
    if (o) return o;
    if (this.movesPlayed >= this.opts.maxMoves) return 'limit';
    const lines = await this.prefetch();
    if (mateIn(lines[0], this.user === 'white') !== undefined) return undefined; // play the mate out
    const good = lines.filter(l => this.good(lines[0], l));
    const quiet = good.length >= 2 && !position(this.fen).isCheck() && !this.lastCapture;
    return quiet ? 'quiet' : undefined;
  }

  /** Whether `l` is good enough next to the engine's `best`. */
  private good(best: ELine, l: ELine): boolean {
    const white = this.user === 'white';
    const target = mateIn(best, white);
    if (target !== undefined) {
      const m = mateIn(l, white);
      return m !== undefined && (!this.opts.strictMate || m <= target);
    }
    return expectation(best, white) - expectation(l, white) <= this.opts.goodMove;
  }

  /** Best move here (for hints), once analysed. */
  best(): string | undefined {
    return this.lines && this.lines[0] ? this.lines[0].uci : undefined;
  }

  /** Judge the user's move; on success, get the opponent's reply and move on. */
  async play(uci: string): Promise<LiveStep> {
    const white = this.user === 'white';
    const lines = await this.prefetch();
    const best = lines[0];
    let mine = lines.find(l => l.uci === uci);
    if (!mine) mine = (await this.engine.analyse(this.fen, 1, [uci]))[0];
    const loss = expectation(best, white) - expectation(mine, white);
    if (!this.good(best, mine))
      return {
        kind: 'failed',
        move: uci,
        san: san(this.fen, uci),
        best: best.uci,
        bestSan: san(this.fen, best.uci),
        loss,
        line: mine,
      };

    this.movesPlayed++;
    const mid = after(this.fen, uci);
    const done = over(mid.fen);
    if (done) return { kind: 'solved', move: uci, reason: done };
    const replyLine = (await this.engine.analyse(mid.fen, 1))[0];
    const reply = replyLine.uci;
    const next = after(mid.fen, reply);
    this.fen = next.fen;
    this.lastCapture = next.capture;
    this.lines = undefined;
    this.pending = undefined;
    const end = await this.endHere();
    if (end) return { kind: 'solved', move: uci, reason: end, reply, replyLine };
    return { kind: 'continue', move: uci, reply, replyLine };
  }
}

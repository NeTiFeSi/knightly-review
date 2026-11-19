// Front-side answer checking: pure logic, no DOM.
//
// The PGN gives the puzzle's first move(s). Where it runs out, a live engine (when
// available) takes over: see live.ts. Live moves are grafted onto the move tree as
// extra nodes (`live: true`), so the move list and navigation work as usual.
import { normalizeMove } from 'chessops/chess';
import { makeFen } from 'chessops/fen';
import { makeSan } from 'chessops/san';
import type { Color, Move, Square } from 'chessops/types';
import { makeSquare, makeUci, parseSquare, parseUci } from 'chessops/util';
import type { ELine, LiveEngine } from './engine';
import { EndReason, LiveLine, LiveOptions } from './live';
import { isAccepted, isRejected } from './nag';
import { KNode, positionOf } from './pgn';

export type FailReason = 'rejected' | 'unknown' | 'gaveup' | 'live';

export type Outcome =
  | { kind: 'continue'; played: KNode; reply: KNode }
  | { kind: 'solved'; played: KNode; reply?: KNode }
  /** The PGN line ended; the live engine takes over (call startLive()). */
  | { kind: 'live'; played: KNode; reply?: KNode }
  | { kind: 'failed'; reason: FailReason; at: KNode; san?: string; node?: KNode; best?: string; bestSan?: string };

/** Engine UCI (castling as e1g1) for a chessops move (castling as king-takes-rook). */
export function engineUci(at: KNode, move: Move): string {
  const pos = positionOf(at);
  if ('from' in move) {
    const piece = pos.board.get(move.from);
    const target = pos.board.get(move.to);
    if (piece && piece.role === 'king' && target && target.color === piece.color && target.role === 'rook') {
      const rank = move.from >> 3;
      const file = (move.to & 7) > (move.from & 7) ? 6 : 2;
      return makeSquare(move.from) + makeSquare(rank * 8 + file);
    }
  }
  return makeUci(move);
}

function evalOf(l?: ELine): KNode['eval'] {
  if (!l) return undefined;
  if (l.mate !== undefined && l.mate !== null) return { mate: l.mate };
  if (l.cp !== undefined && l.cp !== null) return { pawns: l.cp / 100 };
  return undefined;
}

/** Add a live move under `parent` (or return the existing child for it). */
export function graft(parent: KNode, uci: string, line?: ELine): KNode {
  const pos = positionOf(parent);
  const raw = parseUci(uci);
  if (!raw) throw new Error('bad move ' + uci);
  const move = normalizeMove(pos, raw);
  if (!pos.isLegal(move)) throw new Error('illegal move ' + uci);
  const san = makeSan(pos, move);
  pos.play(move);
  const fen = makeFen(pos.toSetup());
  const existing = parent.children.find(c => c.fen === fen);
  if (existing) return existing;
  const node: KNode = {
    id: parent.id + '.' + parent.children.length,
    fen,
    turn: pos.turn,
    ply: parent.ply + 1,
    mainline: false,
    check: pos.isCheck(),
    san,
    uci: makeUci(move),
    move,
    nags: [],
    comment: '',
    startingComment: '',
    eval: evalOf(line),
    shapes: [],
    live: true,
    parent,
    children: [],
  };
  parent.children.push(node);
  return node;
}

export interface LiveSetup {
  engine: LiveEngine;
  options: LiveOptions;
}

/**
 * Moves accepted at `position`. The first move of the puzzle is graded only by
 * its NAG; at later user turns the line's own next move is also accepted
 * unless it carries a rejecting NAG.
 */
export function acceptedMoves(position: KNode, first: boolean, accept: readonly number[]): KNode[] {
  return position.children.filter(
    (c, i) => isAccepted(c.nags, accept) || (!first && i === 0 && !isRejected(c.nags, accept)),
  );
}

export type Hint = { kind: 'text'; text: string } | { kind: 'piece'; squares: Square[] };

export class PuzzleRun {
  status: 'playing' | 'solved' | 'failed' = 'playing';
  /** Position where the user is to move. */
  cur: KNode;
  readonly userColor: Color;
  /** User moves played so far (accepted ones). */
  readonly played: KNode[] = [];
  /** Every node revealed by play (user moves and replies). */
  readonly line: KNode[] = [];
  failure?: Extract<Outcome, { kind: 'failed' }>;
  /** Hints revealed so far, over the whole attempt. */
  hintsUsed = 0;
  /** Hints revealed at the current position (reset when the line advances). */
  hints: Hint[] = [];
  /** Set while playing on with the live engine. */
  live?: LiveLine;
  /** Node where live play started. */
  liveFrom?: KNode;
  /** Engine UCIs played live, user moves and replies in order (the failed move last). */
  readonly liveMoves: string[] = [];
  /** Why live play ended ("unavailable" when the engine failed). */
  liveEnd?: EndReason | 'unavailable';

  constructor(
    readonly start: KNode,
    readonly accept: readonly number[],
    /** Optional text hint for the first move (`[%hint …]`). */
    readonly hintText?: string,
    /** Engine to continue past the PGN with; without it the puzzle ends where the PGN does. */
    public liveSetup?: LiveSetup,
  ) {
    this.cur = start;
    this.userColor = start.turn;
  }

  /** Turn on live play (the engine may become available after the card is shown). */
  enableLive(setup: LiveSetup): void {
    this.liveSetup = setup;
  }

  get isFirst(): boolean {
    return this.cur === this.start;
  }

  accepted(): KNode[] {
    if (this.live) return [];
    return acceptedMoves(this.cur, this.isFirst, this.accept);
  }

  /** Hints available at the current position: text first (puzzle start only), then the piece. */
  private hintLevels(): Hint[] {
    const levels: Hint[] = [];
    if (this.isFirst && this.hintText) levels.push({ kind: 'text', text: this.hintText });
    const squares: Square[] = [];
    this.accepted().forEach(n => {
      if (n.move && 'from' in n.move && squares.indexOf(n.move.from) < 0) squares.push(n.move.from);
    });
    const best = this.live && this.live.best();
    if (best) squares.push(parseSquare(best.slice(0, 2))!);
    if (squares.length) levels.push({ kind: 'piece', squares });
    return levels;
  }

  hasMoreHints(): boolean {
    return this.status === 'playing' && this.hints.length < this.hintLevels().length;
  }

  /** Reveal the next hint at this position. */
  hint(): Hint | undefined {
    if (!this.hasMoreHints()) return undefined;
    const h = this.hintLevels()[this.hints.length];
    this.hints.push(h);
    this.hintsUsed++;
    return h;
  }

  play(move: Move): Outcome {
    if (this.status !== 'playing') throw new Error('puzzle is over');
    if (this.live) throw new Error('live play: use playLive()');
    const pos = positionOf(this.cur);
    if (!pos.isLegal(move)) throw new Error('illegal move');
    const san = makeSan(pos, move);
    pos.play(move);
    const fen = makeFen(pos.toSetup());
    const node = this.cur.children.find(c => c.fen === fen);
    if (!node) return this.fail({ kind: 'failed', reason: 'unknown', at: this.cur, san });
    if (this.accepted().indexOf(node) < 0)
      return this.fail({ kind: 'failed', reason: 'rejected', at: this.cur, san: node.san, node });

    this.played.push(node);
    this.line.push(node);
    const reply = node.children[0];
    if (!reply) return this.endOfLine(node);
    this.line.push(reply);
    if (!reply.children.length) return this.endOfLine(node, reply);
    this.cur = reply;
    this.hints = [];
    return { kind: 'continue', played: node, reply };
  }

  /** The PGN ran out: hand over to the live engine, or finish. */
  private endOfLine(played: KNode, reply?: KNode): Outcome {
    const at = reply || played;
    if (!this.liveSetup || at.turn !== this.userColor) {
      this.status = 'solved';
      return { kind: 'solved', played, reply };
    }
    const capture = !!at.san && at.san.indexOf('x') >= 0;
    this.live = new LiveLine(this.liveSetup.engine, at.fen, this.userColor, this.liveSetup.options, capture);
    this.liveFrom = at;
    this.cur = at;
    this.hints = [];
    this.live.prefetch().catch(() => undefined);
    return { kind: 'live', played, reply };
  }

  /** After a 'live' outcome: decide whether the puzzle already ends here. True when it does. */
  async startLive(): Promise<boolean> {
    const live = this.live!;
    let end: EndReason | 'unavailable' | undefined;
    try {
      end = await live.endHere();
    } catch (_) {
      end = 'unavailable';
    }
    if (end) this.endLive(end);
    return !!end;
  }

  private endLive(reason: EndReason | 'unavailable'): void {
    this.liveEnd = reason;
    this.status = 'solved';
    this.live = undefined;
  }

  /** A user move while playing live. Engine errors end the puzzle as solved ("unavailable"). */
  async playLive(move: Move): Promise<Outcome> {
    const live = this.live;
    if (!live || this.status !== 'playing') throw new Error('not playing live');
    const at = this.cur;
    const pos = positionOf(at);
    if (!pos.isLegal(move)) throw new Error('illegal move');
    const uci = engineUci(at, move);
    let step;
    try {
      step = await live.play(uci);
    } catch (_) {
      const node = graft(at, uci);
      this.liveMoves.push(uci);
      this.played.push(node);
      this.line.push(node);
      this.cur = node;
      this.endLive('unavailable');
      return { kind: 'solved', played: node };
    }
    if (step.kind === 'failed') {
      const node = graft(at, uci, step.line);
      this.liveMoves.push(uci);
      return this.fail({ kind: 'failed', reason: 'live', at, san: step.san, node, best: step.best, bestSan: step.bestSan });
    }
    const node = graft(at, uci);
    this.liveMoves.push(uci);
    this.played.push(node);
    this.line.push(node);
    this.hints = [];
    if (!step.reply) {
      this.cur = node;
      this.endLive(step.kind === 'solved' ? step.reason : 'quiet');
      return { kind: 'solved', played: node };
    }
    const reply = graft(node, step.reply, step.replyLine);
    this.liveMoves.push(step.reply);
    this.line.push(reply);
    this.cur = reply;
    if (step.kind === 'solved') {
      this.endLive(step.reason);
      return { kind: 'solved', played: node, reply };
    }
    return { kind: 'continue', played: node, reply };
  }

  giveUp(): Outcome {
    return this.fail({ kind: 'failed', reason: 'gaveup', at: this.cur });
  }

  private fail(o: Extract<Outcome, { kind: 'failed' }>): Outcome {
    this.status = 'failed';
    this.failure = o;
    this.live = undefined;
    return o;
  }
}

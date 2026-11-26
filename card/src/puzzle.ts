// Front-side answer checking: pure logic, no DOM.
//
// The PGN is the whole solution tree: the user's accepted moves, and the opponent's good replies
// (one is picked, the least seen first). A position that came up by another move order is a leaf in the
// PGN that continues like the position written in full elsewhere (`expandAlias`).
import { makeFen } from 'chessops/fen';
import { makeSan } from 'chessops/san';
import type { Color, Move, Square } from 'chessops/types';
import { parseSquare } from 'chessops/util';
import { isAccepted, isRejected } from './nag';
import { expandAlias, fenKey, KNode, positionOf } from './pgn';

export type FailReason = 'rejected' | 'unknown' | 'gaveup' | 'timeout';

export type Outcome =
  | { kind: 'continue'; played: KNode; reply: KNode }
  /** `draw`: the line ended in a draw (repetition, fifty moves, no material), which counts as solved. */
  | { kind: 'solved'; played: KNode; reply?: KNode; draw?: string }
  | { kind: 'failed'; reason: FailReason; at: KNode; san?: string; node?: KNode };

/** How often each reply position was shown in this card's reviews (see visits.ts). */
export interface Visits {
  get(key: string): number;
  add(key: string): void;
}

/** The reply to show: among `replies`, the ones seen least; ties are broken at random. */
export function pickReply(replies: KNode[], visits?: Visits, random: () => number = Math.random): KNode | undefined {
  if (!replies.length) return undefined;
  const seen = (n: KNode) => (visits ? visits.get(fenKey(n.fen)) : 0);
  const least = Math.min(...replies.map(seen));
  const pool = replies.filter(n => seen(n) === least);
  return pool[Math.min(pool.length - 1, Math.floor(random() * pool.length))];
}

/** Why the position is drawn (the line ends there as solved), given how often it occurred and the clock. */
export function drawReason(node: KNode, occurrences: number, halfmoves: number): string | undefined {
  const pos = positionOf(node);
  if (pos.isStalemate()) return 'Stalemate.';
  if (occurrences >= 3) return 'Draw by threefold repetition.';
  if (halfmoves >= 100) return 'Draw by the fifty-move rule.';
  if (pos.isInsufficientMaterial()) return 'Draw: not enough material.';
  return undefined;
}

/**
 * Forced mate for `color` after the move to `n`: in how many of `color`'s moves,
 * counted from the position before `n` (so a mating move is 1). Undefined when
 * `n` doesn't keep a forced mate for `color` (by its `[%eval #N]`, White's view).
 */
export function mateFor(n: KNode, color: Color): number | undefined {
  if (n.check && n.turn !== color && positionOf(n).isCheckmate()) return 1;
  if (!n.eval || !('mate' in n.eval) || !n.eval.mate) return undefined;
  const m = n.eval.mate;
  return (color === 'white' ? m > 0 : m < 0) ? Math.abs(m) : undefined;
}

/** The fastest forced mate among the moves at `position`, for the side to move. */
export function fastestMate(position: KNode): number | undefined {
  let fastest: number | undefined;
  position.children.forEach(c => {
    const m = mateFor(c, position.turn);
    if (m !== undefined && (fastest === undefined || m < fastest)) fastest = m;
  });
  return fastest;
}

/** Why `node` falls short of a forced mate at `position`: it drops the mate, or (strict) mates slower. */
export type MateMiss = { kind: 'drops'; best: number } | { kind: 'slower'; mate: number; best: number };

export function mateMiss(position: KNode, node: KNode, strictMate: boolean): MateMiss | undefined {
  const best = fastestMate(position);
  if (best === undefined) return undefined;
  const m = mateFor(node, position.turn);
  if (m === undefined) return { kind: 'drops', best };
  if (strictMate && m > best) return { kind: 'slower', mate: m, best };
  return undefined;
}

/**
 * Moves accepted at `position`. The first move of the puzzle is graded only by
 * its NAG; at later user turns the line's own next move is also accepted
 * unless it carries a rejecting NAG.
 *
 * When a move here mates by force, only moves that keep a forced mate count,
 * whatever their NAG (a won position without the mate isn't the answer). With
 * `strictMate`, only the fastest mate does.
 */
export function acceptedMoves(position: KNode, first: boolean, accept: readonly number[], strictMate = false): KNode[] {
  const byNag = position.children.filter(
    (c, i) => isAccepted(c.nags, accept) || (!first && i === 0 && !isRejected(c.nags, accept)),
  );
  const me = position.turn;
  const fastest = fastestMate(position);
  if (fastest === undefined) return byNag;
  const mates = byNag.filter(c => {
    const m = mateFor(c, me);
    return m !== undefined && (!strictMate || m === fastest);
  });
  return mates.length ? mates : byNag; // never leave a puzzle without an answer
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
  /** Why the line ended in a draw, if it did. */
  draw?: string;
  /** Hints revealed so far, over the whole attempt. */
  hintsUsed = 0;
  /** Hints revealed at the current position (reset when the line advances). */
  hints: Hint[] = [];
  /** How often each position (fen key) occurred on the way here, game moves included. */
  private readonly seen = new Map<string, number>();
  /** Half-moves since the last capture or pawn move, along the path actually played. */
  private halfmoves: number;

  constructor(
    readonly start: KNode,
    readonly accept: readonly number[],
    /** Optional text hint for the first move (`[%hint …]`). */
    readonly hintText?: string,
    /** In a forced mate, accept only the fastest mate (any forced mate otherwise). */
    readonly strictMate = false,
    /** Which replies this card showed before (the least seen is shown). */
    readonly visits?: Visits,
    readonly random: () => number = Math.random,
  ) {
    this.cur = start;
    this.userColor = start.turn;
    for (let n: KNode | undefined = start; n; n = n.parent) {
      const k = fenKey(n.fen);
      this.seen.set(k, (this.seen.get(k) || 0) + 1);
    }
    this.halfmoves = positionOf(start).halfmoves;
  }

  get isFirst(): boolean {
    return this.cur === this.start;
  }

  accepted(): KNode[] {
    expandAlias(this.cur);
    return acceptedMoves(this.cur, this.isFirst, this.accept, this.strictMate);
  }

  /** Hints available at the current position: text first (puzzle start only), then the piece. */
  private hintLevels(): Hint[] {
    const levels: Hint[] = [];
    if (this.isFirst && this.hintText) levels.push({ kind: 'text', text: this.hintText });
    const squares: Square[] = [];
    this.accepted().forEach(n => {
      if (n.move && 'from' in n.move && squares.indexOf(n.move.from) < 0) squares.push(n.move.from);
    });
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

  /** Count `node` (just played from `before`) for the draw rules; returns why the game is drawn, if it is. */
  private count(before: KNode, node: KNode): string | undefined {
    const pos = positionOf(before);
    const m = node.move!;
    const reset = 'from' in m && (pos.board.has(m.to) || pos.board.get(m.from)?.role === 'pawn');
    this.halfmoves = reset ? 0 : this.halfmoves + 1;
    const k = fenKey(node.fen);
    const n = (this.seen.get(k) || 0) + 1;
    this.seen.set(k, n);
    return drawReason(node, n, this.halfmoves);
  }

  private solve(played: KNode, reply?: KNode, draw?: string): Outcome {
    this.status = 'solved';
    this.draw = draw;
    return { kind: 'solved', played, reply, draw };
  }

  play(move: Move): Outcome {
    if (this.status !== 'playing') throw new Error('puzzle is over');
    expandAlias(this.cur);
    const pos = positionOf(this.cur);
    if (!pos.isLegal(move)) throw new Error('illegal move');
    const san = makeSan(pos, move);
    pos.play(move);
    const key = fenKey(makeFen(pos.toSetup()));
    const node = this.cur.children.find(c => fenKey(c.fen) === key);
    if (!node) return this.fail({ kind: 'failed', reason: 'unknown', at: this.cur, san });
    if (this.accepted().indexOf(node) < 0)
      return this.fail({ kind: 'failed', reason: 'rejected', at: this.cur, san: node.san, node });

    const before = this.cur;
    this.played.push(node);
    this.line.push(node);
    const drawn = this.count(before, node);
    if (drawn) return this.solve(node, undefined, drawn);
    expandAlias(node);
    const reply = pickReply(node.children, this.visits, this.random);
    if (!reply) return this.solve(node);
    if (this.visits) this.visits.add(fenKey(reply.fen));
    this.line.push(reply);
    const drawnReply = this.count(node, reply);
    expandAlias(reply);
    if (drawnReply || !reply.children.length) return this.solve(node, reply, drawnReply);
    this.cur = reply;
    this.hints = [];
    return { kind: 'continue', played: node, reply };
  }

  giveUp(): Outcome {
    return this.fail({ kind: 'failed', reason: 'gaveup', at: this.cur });
  }

  /** Time ran out (timed puzzles, timeAffects 'fail'). */
  timeout(): Outcome {
    return this.fail({ kind: 'failed', reason: 'timeout', at: this.cur });
  }

  private fail(o: Extract<Outcome, { kind: 'failed' }>): Outcome {
    this.status = 'failed';
    this.failure = o;
    return o;
  }
}

// Front-side answer checking: pure logic, no DOM.
import { makeFen } from 'chessops/fen';
import { makeSan } from 'chessops/san';
import type { Color, Move } from 'chessops/types';
import { isAccepted, isRejected } from './nag';
import { KNode, positionOf } from './pgn';

export type FailReason = 'rejected' | 'unknown' | 'gaveup';

export type Outcome =
  | { kind: 'continue'; played: KNode; reply: KNode }
  | { kind: 'solved'; played: KNode; reply?: KNode }
  | { kind: 'failed'; reason: FailReason; at: KNode; san?: string; node?: KNode };

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

  constructor(
    readonly start: KNode,
    readonly accept: readonly number[],
  ) {
    this.cur = start;
    this.userColor = start.turn;
  }

  get isFirst(): boolean {
    return this.cur === this.start;
  }

  accepted(): KNode[] {
    return acceptedMoves(this.cur, this.isFirst, this.accept);
  }

  play(move: Move): Outcome {
    if (this.status !== 'playing') throw new Error('puzzle is over');
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
    if (!reply) {
      this.status = 'solved';
      return { kind: 'solved', played: node };
    }
    this.line.push(reply);
    if (!reply.children.length) {
      this.status = 'solved';
      return { kind: 'solved', played: node, reply };
    }
    this.cur = reply;
    return { kind: 'continue', played: node, reply };
  }

  giveUp(): Outcome {
    return this.fail({ kind: 'failed', reason: 'gaveup', at: this.cur });
  }

  private fail(o: Extract<Outcome, { kind: 'failed' }>): Outcome {
    this.status = 'failed';
    this.failure = o;
    return o;
  }
}

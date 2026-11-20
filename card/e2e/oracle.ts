// Answers for a card, computed with the card's own PGN and grading code.
// Bundled for Node by e2e/lib/oracle.mjs.
import { makeUci } from 'chessops/util';
import { chessgroundDests } from 'chessops/compat';
import { findPuzzle, KNode, parseGames, positionOf } from '../src/pgn';
import { acceptedMoves } from '../src/puzzle';
import { DEFAULT_ACCEPT } from '../src/nag';

export interface Answer {
  uci: string;
  san: string;
}

export interface Solution {
  n: number;
  /** Side the user plays. */
  color: 'white' | 'black';
  /** Accepted first moves. */
  accepted: Answer[];
  /** The tagged game move: always rejected. */
  rejected: Answer;
  /** A legal first move that is not in the analysis ("not analysed"), if any. */
  unknown?: Answer;
  /** Full line to play for the first accepted move: user moves only, in order. */
  line: Answer[];
}

function answer(n: KNode): Answer {
  return { uci: n.uci || (n.move ? makeUci(n.move) : ''), san: n.san || '' };
}

export function solve(pgn: string, n: number, accept: number[] = DEFAULT_ACCEPT): Solution {
  const puzzle = findPuzzle(parseGames(pgn), n);
  if (!puzzle) throw new Error('no [%kr ' + n + '] in the PGN');
  const start = puzzle.position;
  const first = acceptedMoves(start, true, accept);
  if (!first.length) throw new Error('card ' + n + ' has no accepted move');

  // Follow the first accepted move; the user plays every other ply after it.
  const line: Answer[] = [];
  let node: KNode | undefined = first[0];
  while (node) {
    line.push(answer(node));
    const reply: KNode | undefined = node.children[0];
    if (!reply || !reply.children.length) break;
    node = acceptedMoves(reply, false, accept)[0];
  }

  const known = new Set(start.children.map(c => c.uci));
  let unknown: Answer | undefined;
  const pos = positionOf(start);
  for (const [from, tos] of chessgroundDests(pos)) {
    const to = tos.find(t => !known.has(from + t));
    if (to) {
      unknown = { uci: from + to, san: '' };
      break;
    }
  }

  return { n, color: start.turn, accepted: first.map(answer), rejected: answer(puzzle.gameMove), unknown, line };
}

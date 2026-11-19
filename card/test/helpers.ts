import { readFileSync } from 'node:fs';
import { parseSquare } from 'chessops/util';
import type { Move } from 'chessops/types';
import { normalizeMove } from 'chessops/chess';
import { KNode, parseGames, positionOf } from '../src/pgn';

export const SAMPLES = readFileSync(new URL('../dev/samples.pgn', import.meta.url), 'utf8');
export const games = () => parseGames(SAMPLES);

/** Board-style move (as chessground reports it), normalized like the Board does. */
export function mv(at: KNode, uci: string): Move {
  const from = parseSquare(uci.slice(0, 2))!;
  const to = parseSquare(uci.slice(2, 4))!;
  const roles: Record<string, 'queen' | 'rook' | 'bishop' | 'knight'> = { q: 'queen', r: 'rook', b: 'bishop', n: 'knight' };
  const promo = uci[4] ? roles[uci[4]] : undefined;
  if (promo) return { from, to, promotion: promo };
  return normalizeMove(positionOf(at), { from, to });
}

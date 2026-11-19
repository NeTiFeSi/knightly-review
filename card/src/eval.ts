// Static engine evaluations from `[%eval …]` PGN comment commands.
import type { Evaluation } from 'chessops/pgn';

export type { Evaluation };

const MATE_CP = 100000;

/** Centipawns from White's point of view; mates map to a large value. */
export function evalToCp(ev: Evaluation): number {
  if ('mate' in ev) {
    if (ev.mate === 0) return 0;
    return ev.mate > 0 ? MATE_CP - ev.mate : -MATE_CP - ev.mate;
  }
  return Math.round(ev.pawns * 100);
}

export function formatEval(ev: Evaluation): string {
  if ('mate' in ev) return '#' + ev.mate;
  const p = ev.pawns;
  if (Math.abs(p) < 0.005) return '0.0';
  return (p > 0 ? '+' : '−') + Math.abs(p).toFixed(Math.abs(p) >= 10 ? 0 : 1);
}

/** Share of the bar that belongs to White, 0..1 (Lichess-style win% sigmoid). */
export function whiteShare(ev: Evaluation): number {
  if ('mate' in ev) return ev.mate > 0 ? 1 : ev.mate < 0 ? 0 : 0.5;
  const cp = Math.max(-1000, Math.min(1000, ev.pawns * 100));
  return 1 / (1 + Math.exp(-0.00368208 * cp));
}

/** Remove every `[%cmd …]` (eval, clk, cal, kr, …) and tidy whitespace. */
export function cleanComment(text: string): string {
  return text.replace(/\[%[^\]]*\]/g, ' ').replace(/\s+/g, ' ').trim();
}

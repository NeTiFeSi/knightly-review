// A host-side UCI engine (Stockfish on PATH) to choose moves in live-play scenarios.
import { spawn } from 'node:child_process';

/** Best move (UCI) for `fen`, searched to `depth`. */
export function bestMove(fen, depth = 14, path = process.env.KR_STOCKFISH || 'stockfish') {
  return new Promise((resolve, reject) => {
    const p = spawn(path, [], { stdio: ['pipe', 'pipe', 'ignore'] });
    let buf = '';
    const timer = setTimeout(() => {
      p.kill();
      reject(new Error('stockfish timed out'));
    }, 30000);
    p.on('error', e => {
      clearTimeout(timer);
      reject(e);
    });
    p.stdout.on('data', d => {
      buf += d;
      const m = /^bestmove (\S+)/m.exec(buf);
      if (m) {
        clearTimeout(timer);
        p.stdin.end('quit\n');
        resolve(m[1]);
      }
    });
    p.stdin.write(`uci\nisready\nposition fen ${fen}\ngo depth ${depth}\n`);
  });
}

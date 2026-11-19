// Live engine for continuing a puzzle past the moves stored in the PGN.
//
// Desktop: the Knightly Review add-on runs the user's Stockfish (+ Lc0) and answers
// pycmd('kr:engine:{…}') asynchronously through window.__krEngine.resolve().
// Other clients: a Stockfish WebAssembly build shipped as media (see wasm.ts).

/** One engine line, scores from White's point of view. */
export interface ELine {
  uci: string;
  pv: string[];
  /** Win / draw / loss per mille, White's view (UCI_ShowWDL). */
  wdl: [number, number, number];
  cp?: number;
  mate?: number;
}

export interface LiveEngine {
  readonly name: string;
  /** Lines for `fen`, best first. With `moves`, only those root moves are searched. */
  analyse(fen: string, multipv: number, moves?: string[]): Promise<ELine[]>;
}

/** Expected score 0..100 for one side: (wins + draws / 2) of the engine's WDL. */
export function expectation(l: ELine, white: boolean): number {
  const [w, d, lo] = l.wdl;
  const total = w + d + lo || 1;
  return (100 * ((white ? w : lo) + d / 2)) / total;
}

// ---------------------------------------------------------------------------
// Desktop add-on backend

type PyCmd = (cmd: string, cb?: (r: unknown) => void) => unknown;

interface Pending {
  resolve: (r: ELine[]) => void;
  reject: (e: Error) => void;
  timer: number;
}

interface Bridge {
  pending: Record<number, Pending>;
  next: number;
  resolve(id: number, result: { lines?: ELine[]; error?: string }): void;
}

/** One bridge per page: results arrive through a global the add-on calls. */
function bridge(): Bridge {
  const w = window as unknown as { __krEngine?: Bridge };
  if (!w.__krEngine) {
    const b: Bridge = {
      pending: {},
      next: 1,
      resolve(id, result) {
        const p = b.pending[id];
        if (!p) return; // the card was replaced meanwhile
        delete b.pending[id];
        clearTimeout(p.timer);
        if (result && result.lines) p.resolve(result.lines);
        else p.reject(new Error((result && result.error) || 'engine error'));
      },
    };
    w.__krEngine = b;
  }
  return w.__krEngine;
}

export class DesktopEngine implements LiveEngine {
  readonly name: string;

  constructor(
    name: string,
    private readonly py: PyCmd,
    private readonly timeoutMs = 60000,
  ) {
    this.name = name;
  }

  analyse(fen: string, multipv: number, moves?: string[]): Promise<ELine[]> {
    const b = bridge();
    const id = b.next++;
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        delete b.pending[id];
        reject(new Error('the engine took too long'));
      }, this.timeoutMs);
      b.pending[id] = { resolve, reject, timer };
      try {
        this.py('kr:engine:' + JSON.stringify({ id, fen, multipv, moves: moves || [] }), r => {
          const reply = r as { pending?: boolean; error?: string } | undefined;
          if (!reply || !reply.pending) b.resolve(id, { error: (reply && reply.error) || 'the add-on did not answer' });
        });
      } catch (e) {
        b.resolve(id, { error: String(e) });
      }
    });
  }
}

let cached: Promise<LiveEngine | undefined> | undefined;

/**
 * The live engine for this page, or undefined when none is available.
 * `hello` is the add-on's answer to kr:hello (see settings.ts).
 */
export function liveEngine(hello: () => Promise<unknown>, wasm?: () => Promise<LiveEngine | undefined>): Promise<LiveEngine | undefined> {
  if (!cached)
    cached = hello()
      .then(r => {
        const h = r as { kr?: number; engine?: string } | undefined;
        const py = (window as unknown as { pycmd?: PyCmd }).pycmd;
        if (h && h.kr === 1 && h.engine && typeof py === 'function') {
          try {
            py('kr:warm'); // Lc0 needs a few seconds to start
          } catch (_) {
            /* only a head start */
          }
          return new DesktopEngine(h.engine, py) as LiveEngine;
        }
        return wasm ? wasm() : undefined;
      })
      .catch(() => undefined);
  return cached;
}

/** For tests. */
export function resetLiveEngine(): void {
  cached = undefined;
}

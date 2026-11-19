// Stockfish in the browser (WebAssembly), for clients without the desktop add-on:
// AnkiDroid, AnkiMobile, AnkiWeb, or desktop Anki without the add-on.
//
// The "lite, single-threaded" build of stockfish.js (GPL-3) ships as two media files,
// _kr_stockfish.js and _kr_stockfish.wasm, and runs in a Web Worker. It needs no
// SharedArrayBuffer, so it works in Anki's webviews. Searches are capped by depth and time.
import { parseFen } from 'chessops/fen';
import type { ELine, LiveEngine } from './engine';

export const WASM_JS = '_kr_stockfish.js';
export const WASM_FILE = '_kr_stockfish.wasm';
const DEPTH = 18;
const MOVETIME_MS = 1500;

interface Job {
  fen: string;
  multipv: number;
  moves: string[];
  resolve: (l: ELine[]) => void;
  reject: (e: Error) => void;
}

/** Parse a UCI `info` line into (multipv index, line from the side to move's view). */
export function parseInfo(text: string): { idx: number; line: ELine } | undefined {
  if (!/^info .*\bpv\b/.test(text) || /\b(upper|lower)bound\b/.test(text)) return undefined;
  const num = (re: RegExp) => {
    const m = re.exec(text);
    return m ? parseInt(m[1], 10) : undefined;
  };
  const wdl = /\bwdl (\d+) (\d+) (\d+)/.exec(text);
  const pv = /\bpv (.+)$/.exec(text);
  if (!wdl || !pv) return undefined;
  const moves = pv[1].trim().split(/\s+/);
  return {
    idx: num(/\bmultipv (\d+)/) || 1,
    line: {
      uci: moves[0],
      pv: moves,
      wdl: [+wdl[1], +wdl[2], +wdl[3]],
      cp: num(/\bscore cp (-?\d+)/),
      mate: num(/\bscore mate (-?\d+)/),
    },
  };
}

/** Engine output is from the side to move; ELine is from White's side. */
export function toWhite(line: ELine, whiteToMove: boolean): ELine {
  if (whiteToMove) return line;
  return {
    ...line,
    wdl: [line.wdl[2], line.wdl[1], line.wdl[0]],
    cp: line.cp === undefined ? undefined : -line.cp,
    mate: line.mate === undefined ? undefined : -line.mate,
  };
}

export class WasmEngine implements LiveEngine {
  readonly name = 'Stockfish (built in)';
  private queue: Job[] = [];
  private current?: Job;
  private lines: ELine[] = [];
  private ready: Promise<void>;

  constructor(private readonly worker: Worker) {
    this.ready = new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error('the built-in engine did not start')), 15000);
      let stage = 0;
      worker.onmessage = ev => {
        const text = String(ev.data);
        if (stage === 0 && text === 'uciok') {
          stage = 1;
          worker.postMessage('setoption name UCI_ShowWDL value true');
          worker.postMessage('isready');
        } else if (stage === 1 && text === 'readyok') {
          stage = 2;
          clearTimeout(timer);
          worker.onmessage = e => this.onLine(String(e.data));
          resolve();
        }
      };
      worker.onerror = () => {
        clearTimeout(timer);
        reject(new Error('the built-in engine failed to load'));
      };
      worker.postMessage('uci');
    });
  }

  /** Resolves once the engine answered `uci` and `isready`. */
  started(): Promise<void> {
    return this.ready;
  }

  analyse(fen: string, multipv: number, moves?: string[]): Promise<ELine[]> {
    return new Promise((resolve, reject) => {
      this.queue.push({ fen, multipv, moves: moves || [], resolve, reject });
      this.ready.then(() => this.pump(), reject);
    });
  }

  private pump(): void {
    if (this.current || !this.queue.length) return;
    const job = (this.current = this.queue.shift()!);
    this.lines = [];
    const w = this.worker;
    w.postMessage('setoption name MultiPV value ' + Math.max(1, job.multipv));
    w.postMessage('position fen ' + job.fen);
    w.postMessage(
      'go depth ' + DEPTH + ' movetime ' + MOVETIME_MS + (job.moves.length ? ' searchmoves ' + job.moves.join(' ') : ''),
    );
  }

  private onLine(text: string): void {
    const job = this.current;
    if (!job) return;
    const info = parseInfo(text);
    if (info) {
      this.lines[info.idx - 1] = info.line;
      return;
    }
    if (!text.startsWith('bestmove')) return;
    this.current = undefined;
    const white = parseFen(job.fen).unwrap().turn === 'white';
    const lines = this.lines.filter(Boolean).map(l => toWhite(l, white));
    if (lines.length) job.resolve(lines);
    else job.reject(new Error('no analysis'));
    this.pump();
  }
}

/** Base URL of the collection media: the bundle's own URL, else the document's base. */
export function mediaBase(): string {
  const el = document.getElementById('kr-script') as HTMLScriptElement | null;
  if (el && el.src) return el.src;
  return document.baseURI || location.href;
}

let started: Promise<LiveEngine | undefined> | undefined;

/**
 * Start the engine worker. Anki desktop serves collection media with a strict CSP
 * (no fetch, no script compilation), and a worker loaded from a media URL inherits it.
 * So the script is fetched by the page and run from a Blob URL, which inherits the
 * page's own policy instead; a direct worker is the fallback (other clients).
 */
async function startWorker(): Promise<Worker> {
  const base = mediaBase();
  const js = new URL(WASM_JS, base).href;
  const hash = '#' + encodeURIComponent(new URL(WASM_FILE, base).href);
  try {
    const r = await fetch(js);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const blob = new Blob([await r.text()], { type: 'text/javascript' });
    return new Worker(URL.createObjectURL(blob) + hash);
  } catch (_) {
    return new Worker(js + hash);
  }
}

/** The built-in engine, started once per page; undefined when it can't run here. */
export function wasmEngine(): Promise<LiveEngine | undefined> {
  if (!started)
    started = (async () => {
      if (typeof Worker === 'undefined' || typeof WebAssembly === 'undefined') return undefined;
      let worker: Worker;
      try {
        worker = await startWorker();
      } catch (_) {
        return undefined;
      }
      const engine = new WasmEngine(worker);
      try {
        await engine.started();
        return engine;
      } catch (_) {
        worker.terminate();
        return undefined;
      }
    })();
  return started;
}

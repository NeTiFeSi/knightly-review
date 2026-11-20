// One mounted card (front or back): wires board, eval bar, move list and puzzle.
import { cancelZoom } from './zoom';
import type { DrawShape } from 'chessground/draw';
import type { Key } from 'chessground/types';
import { chessgroundMove } from 'chessops/compat';
import type { Move } from 'chessops/types';
import { makeSquare, makeUci, parseUci } from 'chessops/util';
import { answerCard, canControlReviewer, Ease, showAnswer } from './anki';
import { Board } from './board';
import type { CardRef, Config } from './cardRef';
import { formatEval, whiteShare } from './eval';
import { nagInfo } from './nag';
import {
  allPuzzleMoves,
  annotationsHidden,
  evalHidden,
  shapesHidden,
  findNode,
  findPuzzle,
  KGame,
  KNode,
  parseGames,
  PuzzleRef,
  ViewOptions,
  visibleChildren,
} from './pgn';
import type { LiveEngine } from './engine';
import { acceptedMoves, graft, MateMiss, mateMiss, Outcome, PuzzleRun } from './puzzle';
import { isAccepted } from './nag';
import { cardKey, clearResult, loadResult, saveResult, StoredResult } from './state';

function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function button(cls: string, text: string, title: string, onClick: () => void): HTMLButtonElement {
  const b = h('button', 'kr-btn ' + cls, text);
  b.type = 'button';
  b.title = title;
  b.setAttribute('aria-label', title);
  b.addEventListener('click', e => {
    e.preventDefault();
    onClick();
  });
  return b;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const EASE_LABELS: Record<Ease, string> = { 1: 'Again', 2: 'Hard', 3: 'Good', 4: 'Easy' };

function clampEase(n: number): Ease {
  return Math.max(1, Math.min(4, Math.round(n))) as Ease;
}

/**
 * The ease for an automatic grade. A solve over the time limit (timeAffects 'grade')
 * never grades better than gradeSlow, nor than the hint/solved ease. `slow`: the time
 * lowered the grade.
 */
export function pickEase(r: Pick<StoredResult, 'status' | 'hints' | 'timedOut'>, cfg: Config): { ease: Ease; slow: boolean } {
  if (r.status === 'failed') return { ease: clampEase(cfg.gradeFailed), slow: false };
  const base = clampEase(r.hints ? cfg.gradeHinted : cfg.gradeSolved);
  if (cfg.timed && cfg.timeAffects === 'grade' && r.timedOut) {
    const slow = clampEase(cfg.gradeSlow);
    if (slow < base) return { ease: slow, slow: true };
  }
  return { ease: base, slow: false };
}

/** 18400 → "18 s"; under 10 s one decimal. */
function secs(ms: number): string {
  const s = ms / 1000;
  return (s < 10 ? s.toFixed(1).replace(/\.0$/, '') : String(Math.round(s))) + ' s';
}

export function renderError(root: HTMLElement, msg: string): void {
  root.innerHTML = '';
  root.appendChild(h('div', 'kr-error', 'Knightly Review: ' + msg));
}

export interface AppExtras {
  /** Config problems to show in a banner. */
  warnings: string[];
  /** Resolves to the live engine, or undefined when there is none (see engine.ts). */
  engine?: Promise<LiveEngine | undefined>;
  /** Re-mounted after the settings dialog closed: don't start an automatic grade again. */
  resumed: boolean;
  /** Thinking time (ms) already spent before a re-mount, so the settings dialog can't reset the clock. */
  elapsed?: number;
  openSettings: () => void;
}

export class App {
  private readonly games: KGame[];
  private readonly puzzle: PuzzleRef;
  private readonly view: ViewOptions;
  private readonly key: string;
  private readonly run?: PuzzleRun;
  private readonly result?: StoredResult;
  private cursor: KNode;
  private busy = false;
  /** Front: replaying the game up to the puzzle (intro 'fullGame'). */
  private replaying = false;
  /** Back: automatic grade pending (afterAnswer 'grade'). */
  private gradePending?: { ease: Ease; label: string };
  /** Shown when the reviewer can't be driven from the card. */
  private manualNote?: string;
  /** The user's rejected move, for the red arrow. */
  private failMove?: Move;
  /** Waiting for the live engine. */
  private thinking = false;
  /** Back: show every candidate move at the puzzle position, not only the accepted ones. */
  private showAllCandidates = false;
  private timers: number[] = [];
  /** Timed puzzles (front): thinking time so far, and when the clock last started (undefined while stopped). */
  private elapsed = 0;
  private runningSince?: number;
  /** The intro is over, so the clock may run. */
  private clockArmed = false;
  /** The settings dialog is open. */
  private paused = false;
  private clockTick?: number;
  private timeoutTimer?: number;
  private readonly onVisibility = () => this.syncClock();
  private board!: Board;
  private els!: {
    evalFill: HTMLElement;
    evalText: HTMLElement;
    evalBar: HTMLElement;
    moves: HTMLElement;
    status: HTMLElement;
    backBtn: HTMLButtonElement;
    giveUpBtn?: HTMLButtonElement;
    hintBtn?: HTMLButtonElement;
    prevBtn: HTMLButtonElement;
    nextBtn: HTMLButtonElement;
    clock?: HTMLElement;
    clockText?: HTMLElement;
    clockBar?: HTMLElement;
  };

  static create(root: HTMLElement, card: CardRef, cfg: Config, extras: AppExtras): App | undefined {
    const games = parseGames(card.pgn);
    if (!games.length) {
      renderError(root, 'no readable game in the PGN field.');
      return undefined;
    }
    const puzzle = findPuzzle(games, card.n);
    if (!puzzle) {
      renderError(root, 'no move tagged [%kr ' + card.n + '] in the PGN.');
      return undefined;
    }
    return new App(root, card, cfg, extras, games, puzzle);
  }

  private constructor(
    private readonly root: HTMLElement,
    private readonly card: CardRef,
    private readonly cfg: Config,
    private readonly extras: AppExtras,
    games: KGame[],
    puzzle: PuzzleRef,
  ) {
    this.games = games;
    this.elapsed = extras.elapsed || 0;
    this.puzzle = puzzle;
    this.key = cardKey(card.pgn, card.n);
    this.view = {
      side: card.side,
      puzzle,
      tagged: allPuzzleMoves(games),
      showEval: cfg.showEval,
      evalOnPuzzleFront: cfg.evalOnPuzzleFront,
      revealed: new Set(),
    };
    if (card.side === 'front') {
      clearResult(this.key);
      this.run = new PuzzleRun(puzzle.position, cfg.acceptNags, puzzle.gameMove.hint, undefined, cfg.strictMate);
      if (cfg.live && extras.engine) {
        const run = this.run;
        extras.engine.then(engine => {
          if (engine && run.status === 'playing' && !run.live)
            run.enableLive({ engine, options: { goodMove: cfg.goodMove, maxMoves: cfg.liveMaxMoves, strictMate: cfg.strictMate } });
        });
      }
    } else {
      this.result = loadResult(this.key);
      this.regraft();
    }
    this.cursor = puzzle.position;
    this.build();
    this.start();
  }

  destroy(): void {
    this.clearTimers();
    this.stopClockTimers();
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.board.destroy();
  }

  // -------------------------------------------------------------------------
  // Layout

  private build(): void {
    const root = this.root;
    root.innerHTML = '';
    root.classList.add('kr-root', 'kr-' + this.card.side);
    root.appendChild(this.buildHeader());
    if (this.extras.warnings.length) {
      const w = this.extras.warnings;
      const banner = button(
        'kr-warn-banner',
        '⚠ Card settings: ' + w[0] + (w.length > 1 ? ' (+' + (w.length - 1) + ' more)' : ''),
        'Open the card settings',
        () => this.extras.openSettings(),
      );
      root.appendChild(banner);
    }

    const main = h('div', 'kr-main');
    const col = h('div', 'kr-board-col');
    const row = h('div', 'kr-board-row');

    const evalBar = h('div', 'kr-evalbar');
    const evalFill = h('div', 'kr-evalbar-fill');
    const evalText = h('div', 'kr-evalbar-text');
    evalBar.appendChild(evalFill);
    evalBar.appendChild(evalText);
    if (!this.cfg.showEval) evalBar.style.display = 'none';
    if (this.puzzle.position.turn === 'black') evalBar.classList.add('kr-flip');

    const wrap = h('div', 'kr-board-wrap');
    cancelZoom(wrap, root); // AnkiDroid's html zoom would otherwise shrink the board (see zoom.ts)
    const boardEl = h('div', 'kr-board');
    wrap.appendChild(boardEl);
    row.appendChild(evalBar);
    row.appendChild(wrap);
    let clock: HTMLElement | undefined;
    let clockText: HTMLElement | undefined;
    let clockBar: HTMLElement | undefined;
    if (this.run && this.cfg.timed && this.cfg.showClock) {
      clock = h('div', 'kr-clock');
      clock.setAttribute('aria-hidden', 'true');
      clockText = h('div', 'kr-clock-text');
      clockBar = h('div', 'kr-clock-bar');
      const track = h('div', 'kr-clock-track');
      track.appendChild(clockBar);
      clock.appendChild(clockText);
      clock.appendChild(track);
      col.appendChild(clock);
    }
    col.appendChild(row);

    const controls = h('div', 'kr-controls');
    const prevBtn = button('kr-prev', '◀', 'Previous move (←)', () => this.prev());
    const nextBtn = button('kr-next', '▶', 'Next move (→)', () => this.next());
    const backBtn = button('kr-back-to-puzzle', '↩ Back to puzzle', 'Back to the puzzle position', () =>
      this.goto(this.homeNode()),
    );
    controls.appendChild(prevBtn);
    controls.appendChild(nextBtn);
    controls.appendChild(backBtn);
    let giveUpBtn: HTMLButtonElement | undefined;
    let hintBtn: HTMLButtonElement | undefined;
    if (this.run && this.cfg.hints) {
      hintBtn = button('kr-hint', '💡 Hint', 'Get a hint (shown on the back)', () => this.useHint());
      controls.appendChild(hintBtn);
    }
    if (this.run) {
      giveUpBtn = button('kr-giveup', 'Show answer', 'Give up and show the answer (counts as failed)', () =>
        this.giveUp(),
      );
      controls.appendChild(giveUpBtn);
    }
    col.appendChild(controls);

    const status = h('div', 'kr-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    col.appendChild(status);

    const moves = h('div', 'kr-moves');
    main.appendChild(col);
    main.appendChild(moves);
    root.appendChild(main);

    this.els = { evalFill, evalText, evalBar, moves, status, backBtn, giveUpBtn, hintBtn, prevBtn, nextBtn, clock, clockText, clockBar };
    this.board = new Board(boardEl, this.puzzle.position.turn, this.introStart().fen, m => this.onUserMove(m));
    // Tapping the board (or any control) during the replay skips it.
    boardEl.addEventListener('mousedown', () => this.skipIntro(), true);
    boardEl.addEventListener('touchstart', () => this.skipIntro(), { capture: true, passive: true });
    // Any interaction on the back cancels a pending automatic grade.
    if (this.card.side === 'back') {
      root.addEventListener('mousedown', () => this.cancelGrade(), true);
      root.addEventListener('touchstart', () => this.cancelGrade(), { capture: true, passive: true });
    }    this.drawClock(); // show the full time during the intro too
  }

  private buildHeader(): HTMLElement {
    const hd = this.puzzle.game.headers;
    const head = h('div', 'kr-head');
    head.appendChild(button('kr-gear', '⚙', 'Card settings', () => this.extras.openSettings()));
    const player = (color: 'White' | 'Black') => {
      const name = hd.get(color) || '?';
      const elo = hd.get(color + 'Elo');
      const s = h('span', 'kr-player kr-player-' + color.toLowerCase());
      s.appendChild(h('span', 'kr-dot'));
      s.appendChild(document.createTextNode(name + (elo && elo !== '?' ? ' (' + elo + ')' : '')));
      return s;
    };
    const players = h('div', 'kr-players');
    players.appendChild(player('White'));
    players.appendChild(h('span', 'kr-vs', '–'));
    players.appendChild(player('Black'));
    head.appendChild(players);

    const meta: string[] = [];
    const ev = hd.get('Event');
    const date = hd.get('Date') || hd.get('UTCDate');
    if (ev && ev !== '?') meta.push(ev);
    if (date && !/^\?/.test(date)) meta.push(date.replace(/\.\?\?/g, ''));
    if (this.games.length > 1) meta.push('Game ' + (this.puzzle.game.index + 1) + ' of ' + this.games.length);
    if (meta.length) head.appendChild(h('div', 'kr-meta', meta.join(' · ')));
    const site = hd.get('Site');
    if (site && /^https?:\/\//.test(site)) {
      const a = h('a', 'kr-site', 'View game');
      a.href = site;
      a.target = '_blank';
      a.rel = 'noopener';
      head.lastElementChild!.appendChild(document.createTextNode(meta.length ? ' · ' : ''));
      head.lastElementChild!.appendChild(a);
    }
    return head;
  }

  // -------------------------------------------------------------------------
  // Flow

  /** Mainline nodes from the game start to the puzzle position. */
  private introPath(): KNode[] {
    const path: KNode[] = [];
    for (let n: KNode | undefined = this.puzzle.position; n; n = n.parent) path.unshift(n);
    return path;
  }

  /** Position the board is created with, so the first animation is the intended one. */
  private introStart(): KNode {
    const pos = this.puzzle.position;
    if (this.card.side !== 'front') return pos;
    if (this.cfg.intro === 'fullGame') return this.puzzle.game.root;
    return pos.parent || pos;
  }

  private start(): void {
    if (this.card.side === 'back') {
      this.render();
      if (!this.extras.resumed) this.scheduleGrade();
      return;
    }
    const path = this.introPath();
    if (this.cfg.intro === 'fullGame' && path.length > 2) {
      this.replaying = true;
      this.cursor = path[0];
      this.render();
      path.slice(1).forEach((n, i) =>
        this.later(() => {
          this.cursor = n;
          if (n === this.puzzle.position) {
            this.replaying = false;
            this.armClock();
          }
          this.render();
        }, (i + 1) * this.cfg.introMoveDelay),
      );
      return;
    }
    // Board starts on the position before; animate the opponent's last move.
    this.cursor = this.introStart();
    this.render();
    this.cursor = this.puzzle.position;
    this.later(() => {
      this.render();
      this.armClock();
    }, 250);
  }

  // -------------------------------------------------------------------------
  // Clock (timed puzzles). Timestamps, not a counter, so a phone in the background
  // can't drift; only the user's own thinking time counts.

  private get timed(): boolean {
    return !!this.run && this.cfg.timed;
  }

  private elapsedNow(): number {
    return this.elapsed + (this.runningSince === undefined ? 0 : Date.now() - this.runningSince);
  }

  /** The intro is over: the clock may start. */
  private armClock(): void {
    if (!this.timed || this.clockArmed) return;
    this.clockArmed = true;
    document.addEventListener('visibilitychange', this.onVisibility);
    if (this.els.clock) this.clockTick = window.setInterval(() => this.drawClock(), 250);
    this.syncClock();
  }

  /** Run the clock exactly while the user is to move, and arm or clear the timeout. */
  private syncClock(): void {
    if (!this.timed || !this.clockArmed) return;
    const run = this.run!;
    const should = run.status === 'playing' && !this.busy && !this.replaying && !this.paused && !document.hidden;
    if (should && this.runningSince === undefined) {
      this.runningSince = Date.now();
    } else if (!should && this.runningSince !== undefined) {
      this.elapsed += Date.now() - this.runningSince;
      this.runningSince = undefined;
    }
    if (this.timeoutTimer !== undefined) {
      clearTimeout(this.timeoutTimer);
      this.timeoutTimer = undefined;
    }
    if (should && this.cfg.timeAffects === 'fail')
      this.timeoutTimer = window.setTimeout(() => this.onTimeout(), Math.max(0, this.cfg.timeLimit * 1000 - this.elapsedNow()));
    this.drawClock();
  }

  private stopClockTimers(): void {
    if (this.clockTick !== undefined) clearInterval(this.clockTick);
    if (this.timeoutTimer !== undefined) clearTimeout(this.timeoutTimer);
    this.clockTick = this.timeoutTimer = undefined;
  }

  private drawClock(): void {
    const { clock, clockText, clockBar } = this.els;
    if (!clock || !clockText || !clockBar) return;
    const limit = this.cfg.timeLimit * 1000;
    const left = limit - this.elapsedNow();
    clockText.textContent = left >= 0 ? Math.ceil(left / 1000) + ' s' : '+' + Math.ceil(-left / 1000) + ' s over';
    clockBar.style.width = Math.max(0, Math.min(100, (left / limit) * 100)).toFixed(1) + '%';
    clock.classList.toggle('kr-clock-low', left >= 0 && left <= limit * 0.2);
    clock.classList.toggle('kr-clock-over', left < 0);
    clock.classList.toggle('kr-clock-paused', this.runningSince === undefined && this.run!.status === 'playing');
  }

  private onTimeout(): void {
    this.timeoutTimer = undefined;
    const run = this.run;
    if (!run || run.status !== 'playing' || this.busy) return;
    this.elapsed = this.cfg.timeLimit * 1000;
    this.runningSince = undefined;
    const out = run.timeout();
    if (out.kind === 'failed') this.cursor = out.at;
    this.failMove = undefined;
    this.persist();
    this.render();
    this.finish();
  }

  /** Stop anything time-based the user might miss (settings dialog opening). */
  pause(): void {
    this.paused = true;
    this.skipIntro();
    this.cancelGrade();
    this.syncClock();
  }

  /** The settings dialog closed without a re-mount: carry on. */
  resume(): void {
    this.paused = false;
    this.syncClock();
  }

  /** Thinking time spent so far (ms). */
  elapsedMs(): number {
    return this.elapsedNow();
  }

  /** Thinking time worth carrying into a re-mounted attempt: none once this one is over. */
  carriedElapsedMs(): number | undefined {
    return this.run && this.run.status === 'playing' ? this.elapsedNow() : undefined;
  }

  private skipIntro(): void {
    if (!this.replaying) return;
    this.clearTimers();
    this.replaying = false;
    this.cursor = this.puzzle.position;
    this.armClock();
    this.render();
  }

  private later(f: () => void, ms: number): void {
    this.timers.push(window.setTimeout(f, ms));
  }

  private clearTimers(): void {
    this.timers.forEach(t => clearTimeout(t));
    this.timers = [];
  }

  /** Front: the attempt is over — flip (or not) according to afterAnswer. */
  private finish(): void {
    if (this.cfg.afterAnswer === 'stay') return;
    if (!canControlReviewer()) {
      this.manualNote = 'Show the answer to continue.';
      this.render();
      return;
    }
    this.later(() => {
      if (!showAnswer()) {
        this.manualNote = 'Show the answer to continue.';
        this.render();
      }
    }, this.cfg.flipDelay);
  }

  /** Back: grade automatically after a delay, unless the user interacts. */
  private scheduleGrade(): void {
    const r = this.result;
    if (this.cfg.afterAnswer !== 'grade' || !r || r.graded) return;
    const { ease, slow } = pickEase(r, this.cfg);
    this.gradePending = {
      ease,
      label: EASE_LABELS[ease] + (slow ? ': slower than ' + (r.limitMs !== undefined ? r.limitMs / 1000 : this.cfg.timeLimit) + ' s' : ''),
    };
    this.renderStatus();
    this.later(() => {
      if (!this.gradePending) return;
      this.gradePending = undefined;
      saveResult({ ...r, graded: true });
      if (!answerCard(ease)) {
        this.manualNote = 'Automatic grading is not available here — grade manually.';
        this.renderStatus();
      }
    }, this.cfg.gradeDelay);
  }

  cancelGrade(): void {
    if (!this.gradePending) return;
    this.gradePending = undefined;
    this.manualNote = 'Automatic grading cancelled — grade manually.';
    this.renderStatus();
  }

  private useHint(): void {
    const run = this.run;
    this.skipIntro();
    if (!run || run.status !== 'playing' || this.busy) return;
    if (!run.hint()) return;
    this.cursor = run.cur;
    this.render();
  }

  /** Where "Back to puzzle" goes: the position to answer, or where it ended. */
  private homeNode(): KNode {
    if (this.run && this.run.status === 'playing') return this.run.cur;
    if (this.run && this.run.failure) return this.run.failure.at;
    return this.puzzle.position;
  }

  goto(node: KNode): void {
    this.skipIntro();
    this.cursor = node;
    this.render();
  }

  prev(): void {
    if (this.replaying) return this.skipIntro();
    if (this.cursor.parent) this.goto(this.cursor.parent);
  }

  next(): void {
    if (this.replaying) return this.skipIntro();
    const c = visibleChildren(this.cursor, this.view)[0];
    if (c) this.goto(c);
  }

  /** Back: put the moves played live back onto the tree, so they can be seen and stepped through. */
  private regraft(): void {
    const r = this.result;
    if (!r || !r.live || !r.liveFrom) return;
    let at = findNode(this.games, r.liveFrom);
    try {
      for (const uci of r.live) {
        if (!at) break;
        at = graft(at, uci);
      }
    } catch (_) {
      /* a stale result: show what could be rebuilt */
    }
  }

  private onUserMove(move: Move): void {
    const run = this.run;
    if (!run || run.status !== 'playing' || this.busy) return;
    if (run.live) {
      this.onLiveMove(move);
      return;
    }
    let out: Outcome;
    try {
      out = run.play(move);
    } catch (_) {
      this.render();
      return;
    }
    this.failMove = out.kind === 'failed' ? move : undefined;
    if (out.kind === 'failed') {
      this.cursor = out.at;
      this.persist();
      this.render();
      this.finish();
      return;
    }
    this.view.revealed.add(out.played.id);
    this.cursor = out.played;
    this.busy = true;
    this.render();
    const reply = out.reply;
    if (!reply) {
      this.busy = false;
      this.persist();
      this.render();
      this.finish();
      return;
    }
    this.later(() => {
      this.view.revealed.add(reply.id);
      this.busy = false;
      this.cursor = reply;
      if (out.kind === 'solved') this.persist();
      this.render();
      if (out.kind === 'solved') this.finish();
      if (out.kind === 'live') this.enterLive();
    }, this.cfg.autoReplyDelay);
  }

  /** The PGN line is done: ask the live engine whether to play on. */
  private enterLive(): void {
    const run = this.run!;
    this.think(run.startLive(), true).then(ended => {
      if (ended) {
        this.persist();
        this.render();
        this.finish();
      } else this.render();
    });
  }

  /** Wait for the engine. `redraw`: false keeps the user's just-played move on the board meanwhile. */
  private think<T>(p: Promise<T>, redraw = false): Promise<T> {
    this.thinking = true;
    this.busy = true;
    this.render(redraw);
    const done = () => {
      this.thinking = false;
      this.busy = false;
    };
    return p.then(
      v => {
        done();
        return v;
      },
      e => {
        done();
        throw e;
      },
    );
  }

  private onLiveMove(move: Move): void {
    const run = this.run!;
    // Show the user's move at once, then wait for the engine's verdict.
    this.think(run.playLive(move)).then(out => {
      this.failMove = out.kind === 'failed' ? move : undefined;
      if (out.kind === 'failed') {
        this.cursor = out.at;
        this.persist();
        this.render();
        this.finish();
        return;
      }
      if (out.kind === 'live') return; // not produced by playLive
      this.view.revealed.add(out.played.id);
      this.cursor = out.played;
      const reply = out.reply;
      if (!reply) {
        this.persist();
        this.render();
        this.finish();
        return;
      }
      this.busy = true;
      this.render();
      this.later(() => {
        this.view.revealed.add(reply.id);
        this.busy = false;
        this.cursor = reply;
        if (out.kind === 'solved') this.persist();
        this.render();
        if (out.kind === 'solved') this.finish();
      }, this.cfg.autoReplyDelay);
    });
  }

  private giveUp(): void {
    const run = this.run;
    if (!run || run.status !== 'playing') return;
    const out = run.giveUp();
    if (out.kind === 'failed') this.cursor = out.at;
    this.persist();
    this.render();
    this.finish();
  }

  private persist(): void {
    const run = this.run;
    if (!run || run.status === 'playing') return;
    this.syncClock(); // stops the clock
    this.stopClockTimers();
    this.drawClock();
    const f = run.failure;
    const timed = this.timed && this.clockArmed;
    const elapsedMs = timed ? Math.round(this.elapsed) : undefined;
    saveResult({
      key: this.key,
      status: run.status,
      played: run.played.map(n => n.id),
      reason: f && f.reason,
      failAt: f && f.at.id,
      failSan: f && f.san,
      failNode: f && f.node && f.node.id,
      failUci: this.failMove && makeUci(this.failMove),
      failBest: f && f.best,
      failBestSan: f && f.bestSan,
      failBestMate: f && f.bestMate,
      failMate: f && f.mate,
      liveFrom: run.liveFrom && run.liveFrom.id,
      live: run.liveMoves.length ? run.liveMoves.slice() : undefined,
      liveEnd: run.liveEnd,
      hints: run.hintsUsed,
      elapsedMs,
      limitMs: timed ? this.cfg.timeLimit * 1000 : undefined,
      timedOut: timed ? (f && f.reason === 'timeout') || elapsedMs! > this.cfg.timeLimit * 1000 : undefined,
    });
  }

  // -------------------------------------------------------------------------
  // Rendering

  private render(showBoard = true): void {
    const node = this.cursor;
    const run = this.run;
    const movable = !!run && run.status === 'playing' && !this.busy && !this.replaying && node === run.cur;
    if (showBoard) this.board.show(node, { movable, shapes: this.shapesFor(node) });
    this.root.setAttribute('data-fen', node.fen); // for tests and debugging
    this.renderEval(node);
    this.renderMoves();
    this.renderStatus();
    const home = this.homeNode();
    this.els.backBtn.style.visibility = node === home ? 'hidden' : 'visible';
    this.els.prevBtn.disabled = !node.parent;
    this.els.nextBtn.disabled = !visibleChildren(node, this.view).length;
    this.syncClock();
    if (this.els.giveUpBtn) this.els.giveUpBtn.style.display = run && run.status === 'playing' ? '' : 'none';
    if (this.els.hintBtn) {
      this.els.hintBtn.style.display = run && run.status === 'playing' ? '' : 'none';
      this.els.hintBtn.disabled = !run || !run.hasMoreHints() || this.busy;
    }
  }

  private renderEval(node: KNode): void {
    const { evalBar, evalFill, evalText } = this.els;
    const ev = node.eval;
    const hidden = evalHidden(node, this.view) || annotationsHidden(node, this.view);
    if (!ev || hidden) {
      evalBar.classList.add('kr-evalbar-none');
      evalFill.style.height = '50%';
      evalText.textContent = hidden && ev ? '?' : '';
      evalBar.title = hidden && ev ? 'Evaluation hidden' : 'No evaluation';
      return;
    }
    evalBar.classList.remove('kr-evalbar-none');
    evalFill.style.height = (whiteShare(ev) * 100).toFixed(1) + '%';
    evalText.textContent = formatEval(ev);
    evalBar.title = 'Evaluation: ' + formatEval(ev);
  }

  private arrow(n: KNode, brush: string, label?: string): DrawShape | undefined {
    if (!n.move) return undefined;
    const [orig, dest] = chessgroundMove(n.move) as Key[];
    const s: DrawShape = { orig, dest, brush };
    if (label) s.label = { text: label };
    return s;
  }

  private shapesFor(node: KNode): DrawShape[] {
    const shapes: DrawShape[] = [];
    const push = (s?: DrawShape) => s && shapes.push(s);
    const glyph = (n: KNode) => {
      const i = nagInfo(n.nags);
      return i ? i.glyph : undefined;
    };
    const run = this.run;

    if (run && run.status === 'playing' && node === run.cur && !this.replaying) {
      run.hints.forEach(hint => {
        if (hint.kind === 'piece') hint.squares.forEach(sq => push({ orig: makeSquare(sq) as Key, brush: 'yellow' }));
      });
    } else if (run && run.failure && node === run.failure.at) {
      if (run.failure.best) push(this.moveArrow(parseUci(run.failure.best), 'green'));
      else acceptedMoves(node, node === run.start, this.cfg.acceptNags, this.cfg.strictMate).forEach(n => push(this.arrow(n, 'green', glyph(n))));
      if (this.failMove) push(this.moveArrow(this.failMove, 'red'));
    } else if (!run && node === this.puzzle.position) {
      const accepted = acceptedMoves(node, true, this.cfg.acceptNags, this.cfg.strictMate);
      accepted.forEach(n => push(this.arrow(n, 'green', glyph(n))));
      push(this.arrow(this.puzzle.gameMove, 'red', glyph(this.puzzle.gameMove)));
      const mine = this.userFirstMove();
      if (mine && !accepted.concat([this.puzzle.gameMove]).some(n => n.uci === mine.uci))
        push(this.moveArrow(mine.move, 'blue'));
    } else if (!run && this.result && this.result.failAt === node.id && this.result.failAt !== this.puzzle.position.id) {
      if (this.result.failBest) push(this.moveArrow(parseUci(this.result.failBest), 'green'));
      else acceptedMoves(node, false, this.cfg.acceptNags, this.cfg.strictMate).forEach(n => push(this.arrow(n, 'green', glyph(n))));
      if (this.result.failUci) push(this.moveArrow(parseUci(this.result.failUci), 'red'));
    }
    if (!shapes.length && !shapesHidden(node, this.view))
      node.shapes.forEach(s => push({ orig: makeSquare(s.from) as Key, dest: makeSquare(s.to) as Key, brush: s.color }));
    return shapes;
  }

  private moveArrow(move: Move | undefined, brush: string): DrawShape | undefined {
    if (!move) return undefined;
    const [orig, dest] = chessgroundMove(move) as Key[];
    return { orig, dest, brush };
  }

  /** On the back: the user's first move from the stored result. */
  private userFirstMove(): { uci: string; move: Move | undefined; node?: KNode } | undefined {
    const r = this.result;
    if (!r) return undefined;
    if (r.played.length) {
      const n = findNode(this.games, r.played[0]);
      return n && n.uci ? { uci: n.uci, move: n.move, node: n } : undefined;
    }
    if (r.failAt === this.puzzle.position.id && r.failUci) {
      const n = r.failNode ? findNode(this.games, r.failNode) : undefined;
      return { uci: r.failUci, move: parseUci(r.failUci), node: n };
    }
    return undefined;
  }

  private renderStatus(): void {
    const el = this.els.status;
    el.innerHTML = '';
    el.className = 'kr-status';
    const line = (cls: string, ...parts: (string | Node)[]) => {
      const d = h('div', cls);
      parts.forEach(p => d.appendChild(typeof p === 'string' ? document.createTextNode(p) : p));
      el.appendChild(d);
      return d;
    };
    const toMove = capitalize(this.puzzle.position.turn) + ' to move';
    const run = this.run;

    const note = () => this.manualNote && line('kr-status-sub kr-status-note', this.manualNote);
    const hintCount = (n: number) => n + (n === 1 ? ' hint' : ' hints');

    if (run) {
      if (this.replaying) {
        line('kr-status-main', 'Replaying the game…');
        line('kr-status-sub', 'Tap the board to skip to the puzzle.');
        return;
      }
      if (run.status === 'playing') {
        if (this.thinking) {
          line('kr-status-main', run.live && run.played.length ? 'Checking your move…' : 'The engine is thinking…');
          line('kr-status-sub', 'Playing on with ' + (run.liveSetup ? run.liveSetup.engine.name : 'the engine') + '.');
          return;
        }
        if (run.live && run.played.length) {
          const last = run.played[run.played.length - 1];
          el.classList.add('kr-good');
          line('kr-status-main', '✓ ', this.moveBadge(last), ' — keep going until it’s settled');
        } else if (run.played.length) {
          const last = run.played[run.played.length - 1];
          el.classList.add('kr-good');
          line('kr-status-main', this.moveBadge(last), ' — keep going');
        } else line('kr-status-main', toMove + ' — find the best move.');
        run.hints.forEach(hint =>
          line(
            'kr-status-hint',
            '💡 ' + (hint.kind === 'text' ? hint.text : 'Move the highlighted piece' + (hint.squares.length > 1 ? 's' : '') + '.'),
          ),
        );
        return;
      }
      const again = this.cfg.afterAnswer === 'grade' ? '.' : ' — press Again.';
      if (run.status === 'solved') {
        el.classList.add('kr-good');
        const how = run.hintsUsed ? '✓ Solved with ' + hintCount(run.hintsUsed) + ' — ' : '✓ Solved — ';
        line('kr-status-main', how, this.moveBadge(run.played[0]));
        const end = this.liveEndText(run.liveEnd, run.played.length);
        if (end) line('kr-status-sub', end);
        note();
        return;
      }
      const f = run.failure!;
      el.classList.add('kr-bad');
      if (f.reason === 'gaveup') line('kr-status-main', '✗ Answer shown' + again);
      else if (f.reason === 'timeout') line('kr-status-main', '✗ Time ran out at ' + this.cfg.timeLimit + ' s' + again);
      else if (f.reason === 'unknown') line('kr-status-main', '✗ ' + f.san + ' is not in the analysis' + again);
      else if (f.reason === 'live') {
        const miss = liveMateMiss(f.bestMate, f.mate);
        line('kr-status-main', '✗ ' + f.san + ' ' + (miss ? this.mateMissText(miss) : 'lets the advantage slip') + again);
        if (f.bestSan) line('kr-status-sub', 'Best: ' + f.bestSan);
        note();
        return;
      } else {
        const miss = mateMiss(f.at, f.node!, this.cfg.strictMate);
        // Its NAG may say "! Good": the mate rule is what rejected it, so say that instead.
        if (miss) line('kr-status-main', '✗ ' + f.node!.san + ' ' + this.mateMissText(miss) + again);
        else line('kr-status-main', '✗ ', this.moveBadge(f.node!), again);
        if (f.node!.comment) line('kr-status-comment', f.node!.comment);
      }
      this.acceptedLine(line, f.at, f.at === run.start);
      note();
      return;
    }

    // Back
    const r = this.result;
    const mine = this.userFirstMove();
    if (r) {
      el.classList.add(r.status === 'solved' ? 'kr-good' : 'kr-bad');
      const mark = r.status === 'solved' ? '✓ Solved — you played ' : '✗ You played ';
      if (r.status === 'failed' && r.reason === 'gaveup' && !r.played.length) line('kr-status-main', '✗ Answer shown.');
      else if (r.status === 'failed' && r.reason === 'timeout' && !r.played.length)
        line('kr-status-main', '✗ Time ran out at ' + this.cfg.timeLimit + ' s.');
      else if (mine && mine.node) {
        const miss = r.status === 'failed' && r.reason === 'rejected' && !r.played.length ? mateMiss(this.puzzle.position, mine.node, this.cfg.strictMate) : undefined;
        if (miss) line('kr-status-main', mark + mine.node.san + ', which ' + this.mateMissText(miss) + '.');
        else line('kr-status-main', mark, this.moveBadge(mine.node));
      }
      else if (r.failSan && r.failAt === this.puzzle.position.id) line('kr-status-main', mark + r.failSan + ' (not analysed)');
      if (r.status === 'failed' && r.played.length) {
        const at = r.failAt ? findNode(this.games, r.failAt) : undefined;
        if (r.reason === 'timeout') line('kr-status-sub', 'Then time ran out at ' + this.cfg.timeLimit + ' s.');
        else if (r.reason === 'live') {
          const miss = liveMateMiss(r.failBestMate, r.failMate);
          const what = miss ? (miss.kind === 'drops' ? ' missed the forced mate' : ' mated, but slower') : ' let it slip';
          line('kr-status-sub', 'Then ' + (r.failSan || 'a move') + (at && at.san ? ' after ' + at.san : '') + what + (r.failBestSan ? '; ' + r.failBestSan + ' was best.' : '.'));
        }
        else line('kr-status-sub', 'Then missed ' + (r.failSan || 'the continuation') + (at && at.san ? ' after ' + at.san : '') + '.');
      }
      const end = this.liveEndText(r.liveEnd as never, r.played.length);
      if (end && r.status === 'solved') line('kr-status-sub', end);
      if (r.hints) line('kr-status-sub', 'Used ' + hintCount(r.hints) + '.');
      if (r.elapsedMs !== undefined && r.reason !== 'timeout') {
        const limit = r.limitMs !== undefined ? r.limitMs / 1000 : this.cfg.timeLimit;
        const took = secs(r.elapsedMs);
        line(
          'kr-status-sub',
          r.status === 'solved'
            ? r.timedOut
              ? 'Solved in ' + took + ', over the ' + limit + ' s limit.'
              : 'Solved in ' + took + ' of ' + limit + ' s.'
            : 'Time: ' + took + ' of ' + limit + ' s.',
        );
      }
    } else line('kr-status-main', toMove + '.');
    this.acceptedLine(line, this.puzzle.position, true);
    line('kr-status-sub', 'Game: ', this.moveBadge(this.puzzle.gameMove));
    if (this.puzzle.gameMove.hint) line('kr-status-sub', '💡 Hint: ' + this.puzzle.gameMove.hint);
    if (this.gradePending)
      line('kr-status-sub kr-status-note', 'Marking as ' + this.gradePending.label + '… tap anywhere to cancel.');
    note();
  }

  private liveEndText(reason: string | undefined, moves: number): string | undefined {
    const n = moves + (moves === 1 ? ' move' : ' moves');
    switch (reason) {
      case 'mate':
        return 'Checkmate after ' + n + '.';
      case 'draw':
        return 'Drawn after ' + n + '.';
      case 'quiet':
        return moves > 1 ? 'Played on for ' + n + ' until the position settled.' : undefined;
      case 'limit':
        return 'Played ' + n + ' (the limit).';
      case 'unavailable':
        return 'The engine was not available to play on.';
      default:
        return undefined;
    }
  }

  private acceptedLine(
    line: (cls: string, ...parts: (string | Node)[]) => HTMLElement,
    at: KNode,
    first: boolean,
  ): void {
    const acc = acceptedMoves(at, first, this.cfg.acceptNags, this.cfg.strictMate);
    if (!acc.length) return;
    const parts: (string | Node)[] = ['Best: '];
    acc.forEach((n, i) => {
      if (i) parts.push(', ');
      parts.push(this.moveBadge(n));
    });
    line('kr-status-sub', ...parts);
  }

  /** "misses the forced mate (mate in 1)" / "mates, but slower (mate in 3; mate in 1 was there)". */
  private mateMissText(miss: MateMiss): string {
    const ev = this.cfg.showEval;
    if (miss.kind === 'drops') return 'misses the forced mate' + (ev ? ' (mate in ' + miss.best + ')' : '');
    return 'mates, but slower' + (ev ? ' (mate in ' + miss.mate + '; mate in ' + miss.best + ' was there)' : '');
  }

  /** "Nd5 ! Good +2.1" */
  private moveBadge(n: KNode): HTMLElement {
    const s = h('span', 'kr-badge');
    s.appendChild(h('span', 'kr-badge-san', n.san));
    const info = nagInfo(n.nags);
    if (info) {
      s.classList.add('kr-nag-' + info.cls);
      s.appendChild(h('span', 'kr-badge-nag', info.glyph + ' ' + info.label));
    }
    if (n.eval && this.cfg.showEval) s.appendChild(h('span', 'kr-badge-eval', formatEval(n.eval)));
    return s;
  }

  // -------------------------------------------------------------------------
  // Move list

  private renderMoves(): void {
    const box = this.els.moves;
    box.innerHTML = '';
    const root = this.puzzle.game.root;
    if (root.comment) box.appendChild(h('span', 'kr-comment', root.comment));
    const first = visibleChildren(root, this.view)[0];
    if (first) this.renderSeq(first, box);
    else box.appendChild(h('span', 'kr-empty', 'No moves.'));
    const active = box.querySelector('.kr-active') as HTMLElement | null;
    if (active) {
      const top = active.offsetTop - box.offsetTop;
      if (top < box.scrollTop || top > box.scrollTop + box.clientHeight - 30)
        box.scrollTop = Math.max(0, top - box.clientHeight / 2);
    }
  }

  private renderSeq(first: KNode, out: HTMLElement): void {
    let n: KNode | undefined = first;
    let forceNumber = true;
    while (n) {
      const parent: KNode = n.parent!;
      const hideAnn = annotationsHidden(n, this.view);
      if (n.startingComment && !hideAnn) {
        out.appendChild(h('span', 'kr-comment', n.startingComment));
        forceNumber = true;
      }
      out.appendChild(this.moveEl(n, forceNumber, hideAnn));
      forceNumber = false;
      if (n.comment && !hideAnn) {
        out.appendChild(h('span', 'kr-comment', n.comment));
        forceNumber = true;
      }
      const siblings = visibleChildren(parent, this.view);
      if (siblings[0] === n && siblings.length > 1) {
        const rest = siblings.slice(1);
        const shown = this.collapsible(parent) ? rest.filter(c => this.keepCandidate(c)) : rest;
        for (const s of shown) {
          const v = h('div', 'kr-var');
          this.renderSeq(s, v);
          out.appendChild(v);
        }
        const hidden = rest.length - shown.length;
        if (hidden > 0) {
          const more = button('kr-more', '+ ' + hidden + ' more move' + (hidden > 1 ? 's' : '') + ' (?!, ?, ??)', 'Show every analysed move', () => {
            this.showAllCandidates = true;
            this.renderMoves();
          });
          out.appendChild(more);
        }
        forceNumber = true;
      }
      n = visibleChildren(n, this.view)[0];
    }
  }

  /** Back, at the puzzle position: rejected candidates fold away behind "+ N more". */
  private collapsible(parent: KNode): boolean {
    return this.card.side === 'back' && parent === this.puzzle.position && !this.showAllCandidates;
  }

  private keepCandidate(c: KNode): boolean {
    if (c === this.puzzle.gameMove || c.live || isAccepted(c.nags, this.cfg.acceptNags)) return true;
    const r = this.result;
    if (r && (r.played.indexOf(c.id) >= 0 || r.failNode === c.id)) return true;
    return c === this.cursor || this.isAncestorOf(c, this.cursor);
  }

  private isAncestorOf(a: KNode, b: KNode): boolean {
    for (let n: KNode | undefined = b.parent; n; n = n.parent) if (n === a) return true;
    return false;
  }

  private moveEl(n: KNode, forceNumber: boolean, hideAnn: boolean): HTMLElement {
    const white = n.ply % 2 === 1;
    const num = Math.ceil(n.ply / 2);
    const wrap = h('span', 'kr-move-wrap');
    if (white) wrap.appendChild(h('span', 'kr-num', num + '.'));
    else if (forceNumber) wrap.appendChild(h('span', 'kr-num', num + '…'));
    const m = h('span', 'kr-move');
    m.setAttribute('data-id', n.id);
    m.setAttribute('role', 'button');
    m.tabIndex = -1;
    m.appendChild(h('span', 'kr-san', n.san));
    const info = hideAnn ? undefined : nagInfo(n.nags);
    if (info) {
      m.classList.add('kr-nag-' + info.cls);
      const g = h('span', 'kr-glyph', info.glyph);
      g.title = info.label;
      m.appendChild(g);
    }
    if (n.eval && !evalHidden(n, this.view) && !hideAnn) m.appendChild(h('span', 'kr-ev', formatEval(n.eval)));
    if (n === this.cursor) m.classList.add('kr-active');
    if (n === this.puzzle.gameMove) m.classList.add('kr-puzzle-move');
    if (!n.mainline) m.classList.add('kr-side');
    if (n.live) {
      m.classList.add('kr-live');
      m.title = 'Played with the live engine';
    }
    m.addEventListener('click', () => this.goto(n));
    wrap.appendChild(m);
    return wrap;
  }
}

/** A live failure in a forced mate, from the engine's mate counts. */
function liveMateMiss(best: number | undefined, mate: number | undefined): MateMiss | undefined {
  if (best === undefined || best === null) return undefined;
  if (mate === undefined || mate === null) return { kind: 'drops', best };
  return mate > best ? { kind: 'slower', mate, best } : undefined;
}

// One mounted card (front or back): wires board, eval bar, move list and puzzle.
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
import { acceptedMoves, Outcome, PuzzleRun } from './puzzle';
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

export function renderError(root: HTMLElement, msg: string): void {
  root.innerHTML = '';
  root.appendChild(h('div', 'kr-error', 'Knightly Review: ' + msg));
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
  private timers: number[] = [];
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
  };

  static create(root: HTMLElement, card: CardRef, cfg: Config): App | undefined {
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
    return new App(root, card, cfg, games, puzzle);
  }

  private constructor(
    private readonly root: HTMLElement,
    private readonly card: CardRef,
    private readonly cfg: Config,
    games: KGame[],
    puzzle: PuzzleRef,
  ) {
    this.games = games;
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
      this.run = new PuzzleRun(puzzle.position, cfg.acceptNags, puzzle.gameMove.hint);
    } else {
      this.result = loadResult(this.key);
    }
    this.cursor = puzzle.position;
    this.build();
    this.start();
  }

  destroy(): void {
    this.clearTimers();
    this.board.destroy();
  }

  // -------------------------------------------------------------------------
  // Layout

  private build(): void {
    const root = this.root;
    root.innerHTML = '';
    root.classList.add('kr-root', 'kr-' + this.card.side);
    root.appendChild(this.buildHeader());

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
    const boardEl = h('div', 'kr-board');
    wrap.appendChild(boardEl);
    row.appendChild(evalBar);
    row.appendChild(wrap);
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

    this.els = { evalFill, evalText, evalBar, moves, status, backBtn, giveUpBtn, hintBtn, prevBtn, nextBtn };
    this.board = new Board(boardEl, this.puzzle.position.turn, this.introStart().fen, m => this.onUserMove(m));
    // Tapping the board (or any control) during the replay skips it.
    boardEl.addEventListener('mousedown', () => this.skipIntro(), true);
    boardEl.addEventListener('touchstart', () => this.skipIntro(), { capture: true, passive: true });
    // Any interaction on the back cancels a pending automatic grade.
    if (this.card.side === 'back') {
      root.addEventListener('mousedown', () => this.cancelGrade(), true);
      root.addEventListener('touchstart', () => this.cancelGrade(), { capture: true, passive: true });
    }
  }

  private buildHeader(): HTMLElement {
    const hd = this.puzzle.game.headers;
    const head = h('div', 'kr-head');
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
      this.scheduleGrade();
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
          if (n === this.puzzle.position) this.replaying = false;
          this.render();
        }, (i + 1) * this.cfg.introMoveDelay),
      );
      return;
    }
    // Board starts on the position before; animate the opponent's last move.
    this.cursor = this.introStart();
    this.render();
    this.cursor = this.puzzle.position;
    this.later(() => this.render(), 250);
  }

  private skipIntro(): void {
    if (!this.replaying) return;
    this.clearTimers();
    this.replaying = false;
    this.cursor = this.puzzle.position;
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
    const ease = clampEase(
      r.status === 'failed' ? this.cfg.gradeFailed : r.hints ? this.cfg.gradeHinted : this.cfg.gradeSolved,
    );
    this.gradePending = { ease, label: EASE_LABELS[ease] };
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

  private onUserMove(move: Move): void {
    const run = this.run;
    if (!run || run.status !== 'playing' || this.busy) return;
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
    }, this.cfg.autoReplyDelay);
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
    const f = run.failure;
    saveResult({
      key: this.key,
      status: run.status,
      played: run.played.map(n => n.id),
      reason: f && f.reason,
      failAt: f && f.at.id,
      failSan: f && f.san,
      failNode: f && f.node && f.node.id,
      failUci: this.failMove && makeUci(this.failMove),
      hints: run.hintsUsed,
    });
  }

  // -------------------------------------------------------------------------
  // Rendering

  private render(showBoard = true): void {
    const node = this.cursor;
    const run = this.run;
    const movable = !!run && run.status === 'playing' && !this.busy && !this.replaying && node === run.cur;
    if (showBoard) this.board.show(node, { movable, shapes: this.shapesFor(node) });
    this.renderEval(node);
    this.renderMoves();
    this.renderStatus();
    const home = this.homeNode();
    this.els.backBtn.style.visibility = node === home ? 'hidden' : 'visible';
    this.els.prevBtn.disabled = !node.parent;
    this.els.nextBtn.disabled = !visibleChildren(node, this.view).length;
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
      acceptedMoves(node, node === run.start, this.cfg.acceptNags).forEach(n => push(this.arrow(n, 'green', glyph(n))));
      if (this.failMove) push(this.moveArrow(this.failMove, 'red'));
    } else if (!run && node === this.puzzle.position) {
      const accepted = acceptedMoves(node, true, this.cfg.acceptNags);
      accepted.forEach(n => push(this.arrow(n, 'green', glyph(n))));
      push(this.arrow(this.puzzle.gameMove, 'red', glyph(this.puzzle.gameMove)));
      const mine = this.userFirstMove();
      if (mine && !accepted.concat([this.puzzle.gameMove]).some(n => n.uci === mine.uci))
        push(this.moveArrow(mine.move, 'blue'));
    } else if (!run && this.result && this.result.failAt === node.id && this.result.failAt !== this.puzzle.position.id) {
      acceptedMoves(node, false, this.cfg.acceptNags).forEach(n => push(this.arrow(n, 'green', glyph(n))));
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
        if (run.played.length) {
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
        note();
        return;
      }
      const f = run.failure!;
      el.classList.add('kr-bad');
      if (f.reason === 'gaveup') line('kr-status-main', '✗ Answer shown' + again);
      else if (f.reason === 'unknown') line('kr-status-main', '✗ ' + f.san + ' is not in the analysis' + again);
      else {
        line('kr-status-main', '✗ ', this.moveBadge(f.node!), again);
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
      else if (mine && mine.node) line('kr-status-main', mark, this.moveBadge(mine.node));
      else if (r.failSan && r.failAt === this.puzzle.position.id) line('kr-status-main', mark + r.failSan + ' (not analysed)');
      if (r.status === 'failed' && r.played.length) {
        const at = r.failAt ? findNode(this.games, r.failAt) : undefined;
        line('kr-status-sub', 'Then missed ' + (r.failSan || 'the continuation') + (at && at.san ? ' after ' + at.san : '') + '.');
      }
      if (r.hints) line('kr-status-sub', 'Used ' + hintCount(r.hints) + '.');
    } else line('kr-status-main', toMove + '.');
    this.acceptedLine(line, this.puzzle.position, true);
    line('kr-status-sub', 'Game: ', this.moveBadge(this.puzzle.gameMove));
    if (this.puzzle.gameMove.hint) line('kr-status-sub', '💡 Hint: ' + this.puzzle.gameMove.hint);
    if (this.gradePending)
      line('kr-status-sub kr-status-note', 'Marking as ' + this.gradePending.label + '… tap anywhere to cancel.');
    note();
  }

  private acceptedLine(
    line: (cls: string, ...parts: (string | Node)[]) => HTMLElement,
    at: KNode,
    first: boolean,
  ): void {
    const acc = acceptedMoves(at, first, this.cfg.acceptNags);
    if (!acc.length) return;
    const parts: (string | Node)[] = ['Best: '];
    acc.forEach((n, i) => {
      if (i) parts.push(', ');
      parts.push(this.moveBadge(n));
    });
    line('kr-status-sub', ...parts);
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
        for (const s of siblings.slice(1)) {
          const v = h('div', 'kr-var');
          this.renderSeq(s, v);
          out.appendChild(v);
        }
        forceNumber = true;
      }
      n = visibleChildren(n, this.view)[0];
    }
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
    m.addEventListener('click', () => this.goto(n));
    wrap.appendChild(m);
    return wrap;
  }
}

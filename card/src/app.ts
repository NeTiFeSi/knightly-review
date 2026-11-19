// One mounted card (front or back): wires board, eval bar, move list and puzzle.
import type { DrawShape } from 'chessground/draw';
import type { Key } from 'chessground/types';
import { chessgroundMove } from 'chessops/compat';
import type { Move } from 'chessops/types';
import { makeSquare, makeUci, parseUci } from 'chessops/util';
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
      this.run = new PuzzleRun(puzzle.position, cfg.acceptNags);
    } else {
      this.result = loadResult(this.key);
    }
    this.cursor = puzzle.position;
    this.build();
    this.start();
  }

  destroy(): void {
    this.timers.forEach(t => clearTimeout(t));
    this.timers = [];
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

    this.els = { evalFill, evalText, evalBar, moves, status, backBtn, giveUpBtn, prevBtn, nextBtn };
    this.board = new Board(boardEl, this.puzzle.position.turn, m => this.onUserMove(m));
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

  private start(): void {
    const prev = this.puzzle.position.parent;
    if (this.card.side === 'front' && prev) {
      // Show the position before, then animate the opponent's last move.
      this.board.show(prev, { movable: false });
      this.render(false);
      this.later(() => this.render(), 250);
    } else this.render();
  }

  private later(f: () => void, ms: number): void {
    this.timers.push(window.setTimeout(f, ms));
  }

  /** Where "Back to puzzle" goes: the position to answer, or where it ended. */
  private homeNode(): KNode {
    if (this.run && this.run.status === 'playing') return this.run.cur;
    if (this.run && this.run.failure) return this.run.failure.at;
    return this.puzzle.position;
  }

  goto(node: KNode): void {
    this.cursor = node;
    this.render();
  }

  prev(): void {
    if (this.cursor.parent) this.goto(this.cursor.parent);
  }

  next(): void {
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
      return;
    }
    this.later(() => {
      this.view.revealed.add(reply.id);
      this.busy = false;
      this.cursor = reply;
      if (out.kind === 'solved') this.persist();
      this.render();
    }, this.cfg.autoReplyDelay);
  }

  private giveUp(): void {
    const run = this.run;
    if (!run || run.status !== 'playing') return;
    const out = run.giveUp();
    if (out.kind === 'failed') this.cursor = out.at;
    this.persist();
    this.render();
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
    });
  }

  // -------------------------------------------------------------------------
  // Rendering

  private render(showBoard = true): void {
    const node = this.cursor;
    const run = this.run;
    const movable = !!run && run.status === 'playing' && !this.busy && node === run.cur;
    if (showBoard) this.board.show(node, { movable, shapes: this.shapesFor(node) });
    this.renderEval(node);
    this.renderMoves();
    this.renderStatus();
    const home = this.homeNode();
    this.els.backBtn.style.visibility = node === home ? 'hidden' : 'visible';
    this.els.prevBtn.disabled = !node.parent;
    this.els.nextBtn.disabled = !visibleChildren(node, this.view).length;
    if (this.els.giveUpBtn) this.els.giveUpBtn.style.display = run && run.status === 'playing' ? '' : 'none';
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

    if (run && run.failure && node === run.failure.at) {
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

    if (run) {
      if (run.status === 'playing') {
        if (run.played.length) {
          const last = run.played[run.played.length - 1];
          el.classList.add('kr-good');
          line('kr-status-main', this.moveBadge(last), ' — keep going');
        } else line('kr-status-main', toMove + ' — find the best move.');
        return;
      }
      if (run.status === 'solved') {
        el.classList.add('kr-good');
        line('kr-status-main', '✓ Solved — ', this.moveBadge(run.played[0]));
        return;
      }
      const f = run.failure!;
      el.classList.add('kr-bad');
      if (f.reason === 'gaveup') line('kr-status-main', '✗ Answer shown — press Again.');
      else if (f.reason === 'unknown') line('kr-status-main', '✗ ' + f.san + ' is not in the analysis — press Again.');
      else {
        line('kr-status-main', '✗ ', this.moveBadge(f.node!), ' — press Again.');
        if (f.node!.comment) line('kr-status-comment', f.node!.comment);
      }
      this.acceptedLine(line, f.at, f.at === run.start);
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
    } else line('kr-status-main', toMove + '.');
    this.acceptedLine(line, this.puzzle.position, true);
    line('kr-status-sub', 'Game: ', this.moveBadge(this.puzzle.gameMove));
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

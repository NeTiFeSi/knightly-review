// Thin chessground wrapper: positions from KNodes, legal dests, promotion picker.
import { Chessground } from 'chessground';
import type { Api } from 'chessground/api';
import type { DrawShape } from 'chessground/draw';
import type { Key } from 'chessground/types';
import { chessgroundDests, chessgroundMove } from 'chessops/compat';
import type { Color, Move, Role } from 'chessops/types';
import { parseSquare } from 'chessops/util';
import { normalizeMove } from 'chessops/chess';
import { KNode, positionOf } from './pgn';

export interface ShowOptions {
  movable: boolean;
  shapes?: DrawShape[];
}

const PROMO_ROLES: Role[] = ['queen', 'knight', 'rook', 'bishop'];

export class Board {
  readonly cg: Api;
  private node?: KNode;
  private promo?: HTMLElement;

  constructor(
    private readonly el: HTMLElement,
    orientation: Color,
    private readonly onMove: (move: Move) => void,
  ) {
    this.cg = Chessground(el, {
      orientation,
      coordinates: true,
      animation: { enabled: true, duration: 180 },
      highlight: { lastMove: true, check: true },
      movable: { free: false, showDests: true, events: { after: (o, d) => this.afterMove(o, d) } },
      premovable: { enabled: false },
      drawable: { enabled: true, eraseOnClick: true },
      blockTouchScroll: true,
    });
  }

  show(node: KNode, o: ShowOptions): void {
    this.node = node;
    this.closePromo();
    const pos = positionOf(node);
    this.cg.set({
      fen: node.fen,
      turnColor: node.turn,
      check: node.check ? node.turn : false,
      lastMove: node.move ? (chessgroundMove(node.move) as Key[]) : undefined,
      movable: {
        color: o.movable ? node.turn : undefined,
        dests: o.movable ? (chessgroundDests(pos) as Map<Key, Key[]>) : new Map(),
      },
    });
    this.cg.setAutoShapes(o.shapes || []);
  }

  setShapes(shapes: DrawShape[]): void {
    this.cg.setAutoShapes(shapes);
  }

  destroy(): void {
    this.closePromo();
    this.cg.destroy();
  }

  private afterMove(orig: Key, dest: Key): void {
    const node = this.node;
    if (!node) return;
    const pos = positionOf(node);
    const from = parseSquare(orig)!;
    const to = parseSquare(dest)!;
    const piece = pos.board.get(from);
    const lastRank = node.turn === 'white' ? 7 : 0;
    if (piece && piece.role === 'pawn' && to >> 3 === lastRank) {
      this.askPromotion(dest, node.turn, role => {
        if (role) this.onMove({ from, to, promotion: role });
        else this.show(node, { movable: true });
      });
      return;
    }
    this.onMove(normalizeMove(pos, { from, to }));
  }

  private askPromotion(dest: Key, color: Color, done: (role?: Role) => void): void {
    this.closePromo();
    const wrap = document.createElement('div');
    wrap.className = 'kr-promo';
    const box = document.createElement('div');
    box.className = 'kr-promo-box';
    PROMO_ROLES.forEach(role => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'kr-promo-btn';
      b.setAttribute('aria-label', 'Promote to ' + role);
      const p = document.createElement('piece');
      p.className = role + ' ' + color;
      b.appendChild(p);
      b.addEventListener('click', e => {
        e.stopPropagation();
        this.closePromo();
        done(role);
      });
      box.appendChild(b);
    });
    wrap.appendChild(box);
    wrap.addEventListener('click', () => {
      this.closePromo();
      done(undefined);
    });
    wrap.setAttribute('data-dest', dest);
    this.el.appendChild(wrap);
    this.promo = wrap;
  }

  private closePromo(): void {
    if (this.promo && this.promo.parentNode) this.promo.parentNode.removeChild(this.promo);
    this.promo = undefined;
  }
}

// PGN → annotated move tree, puzzle lookup and per-side visibility rules.
import { Chess, Position } from 'chessops/chess';
import { makeFen, parseFen } from 'chessops/fen';
import {
  CommentShape,
  Node as PgnNode,
  PgnNodeData,
  parseComment,
  parsePgn,
  startingPosition,
} from 'chessops/pgn';
import { makeSanAndPlay, parseSan } from 'chessops/san';
import type { Color, Move } from 'chessops/types';
import { makeUci } from 'chessops/util';
import { cleanComment, Evaluation } from './eval';

export interface KNode {
  /** Path id, unique within the note: "g0", "g0.0", "g0.0.1", … */
  id: string;
  /** FEN of the position after this node's move (or the start position for a root). */
  fen: string;
  /** Side to move in this position. */
  turn: Color;
  /** Ply of this node's move (1 = White's first move); the root's value is that of the last move before it. */
  ply: number;
  /** True if every move from the root to here is the first child. */
  mainline: boolean;
  check: boolean;
  san?: string;
  uci?: string;
  move?: Move;
  nags: number[];
  comment: string;
  startingComment: string;
  eval?: Evaluation;
  shapes: CommentShape[];
  /** `[%hint text]` on a tagged move: optional text hint for its puzzle. */
  hint?: string;
  /** `[%kr N]` tag on this move. */
  puzzleId?: number;
  /**
   * A position written elsewhere in the note (the same fen key, with moves under it): this node continues
   * like it. Set while parsing on leaves, and on the copies `expandAlias` makes.
   */
  alias?: KNode;
  parent?: KNode;
  children: KNode[];
}

export interface KGame {
  index: number;
  headers: Map<string, string>;
  root: KNode;
}

export interface PuzzleRef {
  id: number;
  game: KGame;
  /** Position where the user must find a move. */
  position: KNode;
  /** The tagged mainline move (what was played in the game). */
  gameMove: KNode;
}

const KR_TAG = /\[%kr\s+(\d+)\s*\]/;
const HINT_TAG = /\[%hint\s+([^\]]*)\]/;

export function positionOf(node: KNode): Position {
  return Chess.fromSetup(parseFen(node.fen).unwrap()).unwrap();
}

/**
 * Put every header tag on its own line (outside comments), so games pasted
 * into an Anki field without line breaks still split correctly.
 */
export function normalizePgn(pgn: string): string {
  let out = '';
  let inComment = false;
  let inHeader = false;
  let afterHeader = true; // nothing but headers seen since the last game boundary
  for (let i = 0; i < pgn.length; i++) {
    const c = pgn[i];
    if (inComment) {
      if (c === '}') inComment = false;
    } else if (inHeader) {
      if (c === ']') inHeader = false;
    } else if (c === '[' && /^\[[A-Za-z0-9_]+\s+"/.test(pgn.slice(i, i + 40))) {
      // Headers: one per line; a blank line before a new game's headers.
      out = out.replace(/\s+$/, '') + (afterHeader ? '\n' : '\n\n');
      inHeader = true;
      afterHeader = true;
    } else {
      if (c === '{') inComment = true;
      if (!/\s/.test(c)) {
        if (afterHeader) out = out.replace(/\s+$/, '') + '\n\n';
        afterHeader = false;
      }
    }
    out += c;
  }
  return out.replace(/^\s+/, '');
}

export function parseGames(pgn: string): KGame[] {
  const games: KGame[] = [];
  parsePgn(normalizePgn(pgn)).forEach(g => {
    const start = startingPosition(g.headers);
    if (start.isErr) return;
    const pos = start.value;
    if (!(pos instanceof Chess)) return; // standard chess only
    const index = games.length;
    const root: KNode = {
      id: 'g' + index,
      fen: makeFen(pos.toSetup()),
      turn: pos.turn,
      ply: (pos.fullmoves - 1) * 2 + (pos.turn === 'white' ? 0 : 1),
      mainline: true,
      check: pos.isCheck(),
      nags: [],
      comment: cleanComment((g.comments || []).join(' ')),
      startingComment: '',
      shapes: [],
      children: [],
    };
    build(g.moves, root, pos);
    linkAliases(root);
    games.push({ index, headers: g.headers, root });
  });
  return games;
}

function build(src: PgnNode<PgnNodeData>, parent: KNode, pos: Position): void {
  src.children.forEach(child => {
    const p = pos.clone();
    const move = parseSan(p, child.data.san);
    if (!move) return; // illegal move: cut this branch
    const san = makeSanAndPlay(p, move);
    const raw = (child.data.comments || []).join(' ');
    const parsed = parseComment(raw);
    const tag = KR_TAG.exec(raw);
    const hint = HINT_TAG.exec(raw);
    const node: KNode = {
      id: parent.id + '.' + parent.children.length,
      fen: makeFen(p.toSetup()),
      turn: p.turn,
      ply: parent.ply + 1,
      mainline: parent.mainline && parent.children.length === 0,
      check: p.isCheck(),
      san,
      uci: makeUci(move),
      move,
      nags: child.data.nags ? child.data.nags.slice() : [],
      comment: cleanComment(parsed.text),
      startingComment: cleanComment((child.data.startingComments || []).join(' ')),
      eval: parsed.evaluation,
      shapes: parsed.shapes,
      puzzleId: tag ? parseInt(tag[1], 10) : undefined,
      hint: hint ? hint[1].trim() : undefined,
      parent,
      children: [],
    };
    parent.children.push(node);
    build(child, node, p);
  });
}

/** FEN without the move counters: positions equal whatever move order reached them. */
export function fenKey(fen: string): string {
  return fen.split(' ').slice(0, 4).join(' ');
}

/** Mark each leaf whose position is also written in full elsewhere in the game as an alias of that node. */
function linkAliases(root: KNode): void {
  const written = new Map<string, KNode>();
  for (const n of walkNodes(root)) if (n.children.length && !written.has(fenKey(n.fen))) written.set(fenKey(n.fen), n);
  for (const n of walkNodes(root)) {
    if (n.children.length || n === root) continue;
    const other = written.get(fenKey(n.fen));
    if (other && other !== n) n.alias = other;
  }
}

/**
 * Give an alias leaf the continuation of the node it stands for: copies of its children (themselves aliases
 * until their own children are needed), so the move list shows the path actually played. Idempotent.
 */
export function expandAlias(node: KNode): void {
  const src = node.alias;
  if (!src || node.children.length) return;
  src.children.forEach((c, i) => {
    node.children.push({
      id: node.id + '.' + i,
      fen: c.fen,
      turn: c.turn,
      ply: node.ply + 1,
      mainline: false,
      check: c.check,
      san: c.san,
      uci: c.uci,
      move: c.move,
      nags: c.nags.slice(),
      comment: c.comment,
      startingComment: c.startingComment,
      eval: c.eval,
      shapes: c.shapes,
      parent: node,
      alias: c.children.length ? c : c.alias,
      children: [],
    });
  });
}

export function* walkNodes(node: KNode): Generator<KNode> {
  yield node;
  for (const c of node.children) yield* walkNodes(c);
}

/** The node with this path id; the copies alias leaves make are rebuilt on the way. */
export function findNode(games: KGame[], id: string): KNode | undefined {
  const parts = id.split('.');
  const game = games.find(g => g.root.id === parts[0]);
  let n = game && game.root;
  for (const p of parts.slice(1)) {
    if (!n) return undefined;
    expandAlias(n);
    n = n.children[parseInt(p, 10)];
  }
  return n;
}

export function findPuzzle(games: KGame[], id: number): PuzzleRef | undefined {
  for (const game of games)
    for (const n of walkNodes(game.root))
      if (n.puzzleId === id && n.parent) return { id, game, position: n.parent, gameMove: n };
  return undefined;
}

/** All tagged moves in the note (any card). */
export function allPuzzleMoves(games: KGame[]): Set<KNode> {
  const out = new Set<KNode>();
  for (const g of games) for (const n of walkNodes(g.root)) if (n.puzzleId !== undefined) out.add(n);
  return out;
}

export function mainlineEnd(node: KNode): KNode {
  while (node.children.length) node = node.children[0];
  return node;
}

// ---------------------------------------------------------------------------
// Visibility

export type Side = 'front' | 'back';

export interface ViewOptions {
  side: Side;
  puzzle: PuzzleRef;
  /** Tagged moves of every card in the note. */
  tagged: Set<KNode>;
  showEval: boolean;
  evalOnPuzzleFront: boolean;
  /** Front only: nodes the user has played/seen while solving. */
  revealed: Set<string>;
}

export function visibleChildren(node: KNode, o: ViewOptions): KNode[] {
  if (o.side === 'front')
    return node.children.filter(
      (c, i) =>
        (i === 0 && node.mainline && node.ply < o.puzzle.position.ply) || o.revealed.has(c.id),
    );
  // Back: the mainline, plus everything below this card's puzzle position.
  if (node.mainline && node !== o.puzzle.position) return node.children.slice(0, 1);
  return node.children.slice();
}

/** Hide the NAG of a tagged move that belongs to another card (the front shows them all). */
export function nagHidden(node: KNode, o: ViewOptions): boolean {
  return o.side === 'back' && o.tagged.has(node) && node !== o.puzzle.gameMove;
}

/** Hide the comment (and eval, arrows) of a tagged move: it would hint at a card's answer. */
export function annotationsHidden(node: KNode, o: ViewOptions): boolean {
  if (!o.tagged.has(node)) return false;
  return o.side === 'front' || node !== o.puzzle.gameMove;
}

/**
 * Evals at a puzzle position or on its tagged move hint that a tactic exists.
 * They are hidden for every card in the note, except this card's on the back
 * (or on the front too when `evalOnPuzzleFront` is set).
 */
/** Drawn arrows/circles (`[%cal]`, `[%csl]`) follow the same rule as evals. */
export function shapesHidden(node: KNode, o: ViewOptions): boolean {
  if (annotationsHidden(node, o)) return true;
  const mine = node === o.puzzle.position || node === o.puzzle.gameMove;
  if (mine && o.side === 'back') return false;
  for (const t of o.tagged) if (node === t || node === t.parent) return true;
  return false;
}

export function evalHidden(node: KNode, o: ViewOptions): boolean {
  if (!o.showEval) return true;
  const mine = node === o.puzzle.position || node === o.puzzle.gameMove;
  if (mine && (o.side === 'back' || o.evalOnPuzzleFront)) return false;
  for (const t of o.tagged) if (node === t || node === t.parent) return true;
  return false;
}

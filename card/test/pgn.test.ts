import { describe, expect, it } from 'vitest';
import {
  allPuzzleMoves,
  annotationsHidden,
  evalHidden,
  shapesHidden,
  findPuzzle,
  KNode,
  parseGames,
  ViewOptions,
  visibleChildren,
  walkNodes,
} from '../src/pgn';
import { games, SAMPLES } from './helpers';

function view(side: 'front' | 'back', n: number, extra: Partial<ViewOptions> = {}): ViewOptions {
  const gs = games();
  return {
    side,
    puzzle: findPuzzle(gs, n)!,
    tagged: allPuzzleMoves(gs),
    showEval: true,
    evalOnPuzzleFront: false,
    revealed: new Set(),
    ...extra,
  };
}

/** Every node reachable through visibleChildren. */
function visible(o: ViewOptions): KNode[] {
  const out: KNode[] = [];
  const go = (n: KNode) => {
    out.push(n);
    visibleChildren(n, o).forEach(go);
  };
  go(o.puzzle.game.root);
  return out;
}

describe('parsing', () => {
  it('reads multiple games with headers', () => {
    const gs = games();
    expect(gs).toHaveLength(3);
    expect(gs[1].headers.get('White')).toBe('opponent2');
    expect(gs[2].root.turn).toBe('white');
  });
  it('handles headers and games on one line (Anki field without line breaks)', () => {
    const gs = parseGames('[White "a"] [Black "b"] 1. e4 e5 * [White "c"] 1. d4 *');
    expect(gs.map(g => g.headers.get('White'))).toEqual(['a', 'c']);
  });
  it('does not split on header-like text inside comments', () => {
    const [g] = parseGames('[White "a"] 1. e4 { see [Note "x"] } e5 *');
    expect(g.root.children[0].comment).toBe('see [Note "x"]');
    expect(g.root.children[0].children[0].san).toBe('e5');
  });
  it('supports a FEN start position', () => {
    const [g] = parseGames('[SetUp "1"] [FEN "4k3/P7/8/8/8/8/8/4K3 w - - 0 40"] 40. a8=Q+ *');
    expect(g.root.ply).toBe(78);
    expect(g.root.children[0].ply).toBe(79);
    expect(g.root.children[0].san).toBe('a8=Q+');
  });
  it('cuts illegal branches but keeps the rest', () => {
    const [g] = parseGames('1. e4 (1. e5) e5 *');
    expect(g.root.children.map(c => c.san)).toEqual(['e4']);
    expect(g.root.children[0].children[0].san).toBe('e5');
  });
  it('gives each node a unique id across games', () => {
    const ids = games().flatMap(g => Array.from(walkNodes(g.root), n => n.id));
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('findPuzzle', () => {
  it('finds the tagged move by number, not by order', () => {
    const gs = games();
    const p1 = findPuzzle(gs, 1)!;
    const p2 = findPuzzle(gs, 2)!;
    expect(p1.gameMove.san).toBe('Bxd1');
    expect(p2.gameMove.san).toBe('Bg4');
    expect(p1.game).toBe(p2.game);
    expect(p1.position).toBe(p1.gameMove.parent);
    expect(findPuzzle(gs, 4)!.game.index).toBe(2);
    expect(findPuzzle(gs, 9)).toBeUndefined();
  });
});

describe('visibility', () => {
  it('front shows the mainline only up to the puzzle position', () => {
    const o = view('front', 1);
    const vis = visible(o);
    expect(vis.every(n => n.mainline)).toBe(true);
    expect(vis[vis.length - 1]).toBe(o.puzzle.position);
    expect(vis).not.toContain(o.puzzle.gameMove);
  });
  it('front does not leak the game after a played game move', () => {
    const o = view('front', 1);
    o.revealed.add(o.puzzle.gameMove.id);
    const vis = visible(o);
    expect(vis[vis.length - 1]).toBe(o.puzzle.gameMove);
  });
  it('front reveals nodes the user played', () => {
    const o = view('front', 1);
    const dxe5 = o.puzzle.position.children[1];
    o.revealed.add(dxe5.id);
    expect(visible(o)).toContain(dxe5);
  });
  it("back shows the mainline plus only this card's variations", () => {
    const o = view('back', 1);
    const vis = visible(o);
    const side = vis.filter(n => !n.mainline).map(n => n.san);
    expect(side).toEqual(['dxe5', 'Qxg4', 'Be6', 'Bxe6']);
  });
  it('hides annotations of tagged moves on the front, and of other cards on the back', () => {
    const f = view('front', 1);
    expect(annotationsHidden(f.puzzle.gameMove, f)).toBe(true);
    const b = view('back', 1);
    expect(annotationsHidden(b.puzzle.gameMove, b)).toBe(false);
    const other = Array.from(b.tagged).find(n => n.puzzleId === 2)!;
    expect(annotationsHidden(other, b)).toBe(true);
    expect(annotationsHidden(b.puzzle.position, b)).toBe(false);
  });
  it('hides evals around every puzzle on the front, only other cards on the back', () => {
    const f = view('front', 1);
    expect(evalHidden(f.puzzle.position, f)).toBe(true);
    expect(evalHidden(f.puzzle.gameMove, f)).toBe(true);
    expect(evalHidden(f.puzzle.game.root.children[0], f)).toBe(false);
    const b = view('back', 1);
    expect(evalHidden(b.puzzle.position, b)).toBe(false);
    expect(evalHidden(b.puzzle.gameMove, b)).toBe(false);
    const other = Array.from(b.tagged).find(n => n.puzzleId === 2)!;
    expect(evalHidden(other, b)).toBe(true);
    expect(evalHidden(other.parent!, b)).toBe(true);
  });
  it('hides drawn shapes at puzzle positions on the front', () => {
    const f = view('front', 1);
    expect(f.puzzle.position.shapes.length).toBe(2); // [%cal] on 5. Nxe5
    expect(shapesHidden(f.puzzle.position, f)).toBe(true);
    const b = view('back', 1);
    expect(shapesHidden(b.puzzle.position, b)).toBe(false);
  });
  it('evalOnPuzzleFront and showEval switches', () => {
    const f = view('front', 1, { evalOnPuzzleFront: true });
    expect(evalHidden(f.puzzle.position, f)).toBe(false);
    const off = view('back', 1, { showEval: false });
    expect(evalHidden(off.puzzle.game.root.children[0], off)).toBe(true);
  });
  it('same game in another note can tag other moves', () => {
    const other = SAMPLES.replace('[%kr 1]', '').replace('[%kr 2]', '[%kr 7]');
    const gs = parseGames(other);
    expect(findPuzzle(gs, 1)).toBeUndefined();
    expect(findPuzzle(gs, 7)!.gameMove.san).toBe('Bg4');
  });
});

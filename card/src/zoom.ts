// AnkiDroid (2.25+, old reviewer) sets CSS `zoom` on <html> (e.g. 0.9, from its card-zoom setting).
// Chessground measures its wrapper with getBoundingClientRect() (zoomed pixels) and then applies
// that size as CSS pixels (zoomed again), so under zoom the board shrinks inside its wrapper and
// the pieces drift off the squares. A compensating `zoom` on the board wrapper makes the board
// subtree render 1:1, so both agree. The rest of the card keeps the user's zoom.

/** The product of the CSS zoom of `el` and all its ancestors (1 when there is none). */
export function effectiveZoom(el: Element | null): number {
  let z = 1;
  for (let e: Element | null = el; e; e = e.parentElement) {
    const v = parseFloat(getComputedStyle(e).zoom);
    if (isFinite(v) && v > 0) z *= v;
  }
  return z;
}

/** Make `wrap` (already attached under `parent`'s tree) cancel the zoom it inherits. */
export function cancelZoom(wrap: HTMLElement, parent: Element): void {
  const z = effectiveZoom(parent);
  if (Math.abs(z - 1) > 0.001) wrap.style.zoom = String(1 / z);
}

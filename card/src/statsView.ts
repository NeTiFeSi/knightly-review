// The back's Notes field holds a stats section per card of the note (`data-card`): show the one that belongs to
// the card on screen. Pure DOM, so it is tested without a page.

/** Show the `.kr-stats` sections numbered `n` (the card's cloze number) and hide the others. */
export function showStatsFor(root: ParentNode, n: number): void {
  root.querySelectorAll<HTMLElement>('.kr-stats[data-card]').forEach(el => {
    const mine = Number(el.getAttribute('data-card')) === n;
    if (mine) el.removeAttribute('hidden');
    else el.setAttribute('hidden', '');
  });
}

import { describe, expect, it } from 'vitest';
import { showStatsFor } from '../src/statsView';

// No DOM in these tests: a section is anything with getAttribute / setAttribute / removeAttribute.
function section(card: number) {
  const attrs: Record<string, string> = { 'data-card': String(card) };
  return {
    attrs,
    getAttribute: (k: string) => attrs[k] ?? null,
    setAttribute: (k: string, v: string) => void (attrs[k] = v),
    removeAttribute: (k: string) => void delete attrs[k],
  };
}
const rootOf = (sections: ReturnType<typeof section>[]) =>
  ({ querySelectorAll: () => sections }) as unknown as ParentNode;

describe('showStatsFor', () => {
  it('shows only the section of the active card', () => {
    const [one, two] = [section(1), section(2)];
    const root = rootOf([one, two]);
    showStatsFor(root, 2);
    expect('hidden' in one.attrs).toBe(true);
    expect('hidden' in two.attrs).toBe(false);
    showStatsFor(root, 1);
    expect('hidden' in one.attrs).toBe(false);
    expect('hidden' in two.attrs).toBe(true);
  });

  it('leaves notes without sections alone', () => {
    expect(() => showStatsFor(rootOf([]), 1)).not.toThrow();
  });
});

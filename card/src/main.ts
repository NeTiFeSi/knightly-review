// Bundle entry. Exposed as the global `KnightlyReview`; the templates call mount().
import { App, renderError } from './app';
import { readCard, readConfig } from './cardRef';
import css from './styles.css';
import baseCss from 'chessground/assets/chessground.base.css';
import boardCss from 'chessground/assets/chessground.brown.css';
import piecesCss from 'chessground/assets/chessground.cburnett.css';

let current: App | undefined;
let keysBound = false;

function injectStyles(): void {
  if (document.getElementById('kr-style')) return;
  const style = document.createElement('style');
  style.id = 'kr-style';
  style.textContent = [baseCss, boardCss, piecesCss, css].join('\n');
  (document.head || document.documentElement).appendChild(style);
}

function bindKeys(): void {
  if (keysBound) return;
  keysBound = true;
  document.addEventListener('keydown', e => {
    if (!current || !document.getElementById('kr-root')) return;
    const t = e.target as HTMLElement | null;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'ArrowLeft') {
      current.prev();
      e.preventDefault();
    } else if (e.key === 'ArrowRight') {
      current.next();
      e.preventDefault();
    }
  });
}

/** Mount (or re-mount) the card currently in the DOM. Safe to call repeatedly. */
export function mount(): void {
  if (current) {
    try {
      current.destroy();
    } catch (_) {
      /* old DOM may be gone */
    }
    current = undefined;
  }
  const root = document.getElementById('kr-root');
  if (!root) return;
  injectStyles();
  bindKeys();
  try {
    const card = readCard(root);
    if (typeof card === 'string') return renderError(root, card);
    current = App.create(root, card, readConfig());
  } catch (err) {
    renderError(root, String(err && (err as Error).message ? (err as Error).message : err));
  }
}

export const version = '0.1.0';

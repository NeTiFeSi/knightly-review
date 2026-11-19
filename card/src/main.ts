// Bundle entry. Exposed as the global `KnightlyReview`; the templates call mount().
import { App, renderError } from './app';
import { checkConfig, configDiff, DEFAULT_CONFIG, readCard } from './cardRef';
import { liveEngine } from './engine';
import { wasmEngine } from './wasm';
import { addonAvailable, addonHello, isDesktop, loadOverrides, saveOverrides, storageWorks } from './settings';
import { SettingsPanel } from './settingsPanel';
import css from './styles.css';
import baseCss from 'chessground/assets/chessground.base.css';
import boardCss from 'chessground/assets/chessground.brown.css';
import piecesCss from 'chessground/assets/chessground.cburnett.css';

let current: App | undefined;
let panel: SettingsPanel | undefined;
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
    if (panel) {
      // Focus may have left the dialog (e.g. a re-rendered button), so Escape is handled here.
      if (e.key === 'Escape') {
        e.preventDefault();
        panel.close();
      }
      return;
    }
    const t = e.target as HTMLElement | null;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    current.cancelGrade();
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

/** Template config, then this device's overrides on top; both validated. */
function loadConfig() {
  const raw = window.KR_CONFIG;
  // The template resets KR_CONFIG to null before its config script, so null means that script failed.
  const tpl = checkConfig(raw, DEFAULT_CONFIG, 'Template KR_CONFIG');
  const dev = checkConfig(loadOverrides(), tpl.config, 'Device settings');
  const overrides = configDiff(dev.config, tpl.config);
  if (dev.warnings.length) saveOverrides(overrides); // drop stale entries
  return { template: tpl.config, overrides, config: dev.config, warnings: tpl.warnings.concat(dev.warnings) };
}

/**
 * Mount (or re-mount) the card currently in the DOM. Safe to call repeatedly.
 * `resumed`: re-mounted after the settings dialog, so don't start an automatic grade again.
 */
export function mount(opts?: { resumed?: boolean }): void {
  if (panel) {
    const p = panel;
    panel = undefined;
    if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
  }
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
    const cfg = loadConfig();
    current = App.create(root, card, cfg.config, {
      warnings: cfg.warnings,
      engine: card.side === 'front' && cfg.config.live ? liveEngine(addonHello, wasmEngine) : undefined,
      resumed: !!(opts && opts.resumed),
      openSettings: () => openSettings(root, cfg),
    });
  } catch (err) {
    renderError(root, String(err && (err as Error).message ? (err as Error).message : err));
  }
}

function openSettings(root: HTMLElement, cfg: ReturnType<typeof loadConfig>): void {
  if (panel) return;
  if (current) current.pause();
  const p = new SettingsPanel({
    template: cfg.template,
    overrides: cfg.overrides,
    warnings: cfg.warnings,
    storageOk: storageWorks(),
    desktop: isDesktop(),
    addon: addonAvailable(),
    onClose: changed => {
      if (panel !== p) return;
      panel = undefined;
      if (changed) mount({ resumed: true });
    },
  });
  panel = p;
  root.appendChild(p.el);
  p.focus();
}

export const version = '0.1.0';

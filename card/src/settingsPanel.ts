// The ⚙ settings dialog shown over the card. Only selects, checkboxes, sliders
// and buttons: text inputs would fight Anki's reviewer shortcuts.
import { CONFIG_KEYS, Config, configDiff, DEFAULT_CONFIG, formatConfig } from './cardRef';
import { NAGS } from './nag';
import { clearOverrides, saveOverrides, saveToTemplate } from './settings';

type Field =
  | { key: keyof Config; label: string; kind: 'select'; options: [string | number, string][]; help?: string }
  | { key: keyof Config; label: string; kind: 'toggle'; help?: string }
  | { key: keyof Config; label: string; kind: 'range'; min: number; max: number; step: number; help?: string; format?: (v: number) => string }
  | { key: keyof Config; label: string; kind: 'nags'; help?: string };

interface Group {
  title: string;
  fields: (Field & { when?: (c: Config) => boolean })[];
}

const EASES: [number, string][] = [
  [1, 'Again'],
  [2, 'Hard'],
  [3, 'Good'],
  [4, 'Easy'],
];

const GROUPS: Group[] = [
  {
    title: 'Start of the card',
    fields: [
      {
        key: 'intro',
        label: 'Show the game',
        kind: 'select',
        options: [
          ['lastMove', 'Last move only'],
          ['fullGame', 'Replay the full game'],
        ],
      },
      { key: 'introMoveDelay', label: 'Replay speed (per move)', kind: 'range', min: 300, max: 3000, step: 100, when: c => c.intro === 'fullGame' },
    ],
  },
  {
    title: 'After answering',
    fields: [
      {
        key: 'afterAnswer',
        label: 'Then',
        kind: 'select',
        options: [
          ['flip', 'Show the back'],
          ['stay', 'Stay on the front'],
          ['grade', 'Show the back and grade automatically'],
        ],
      },
      { key: 'flipDelay', label: 'Wait before showing the back', kind: 'range', min: 0, max: 5000, step: 100, when: c => c.afterAnswer !== 'stay' },
      { key: 'gradeDelay', label: 'Wait before grading', kind: 'range', min: 500, max: 10000, step: 500, when: c => c.afterAnswer === 'grade', help: 'Any tap or key cancels the automatic grade.' },
      { key: 'gradeSolved', label: 'Grade when solved', kind: 'select', options: EASES, when: c => c.afterAnswer === 'grade' },
      { key: 'gradeHinted', label: 'Grade when solved with hints', kind: 'select', options: EASES, when: c => c.afterAnswer === 'grade' },
      { key: 'gradeFailed', label: 'Grade when failed', kind: 'select', options: EASES, when: c => c.afterAnswer === 'grade' },
    ],
  },
  {
    title: 'Answers',
    fields: [
      { key: 'acceptNags', label: 'Moves that count as correct', kind: 'nags' },
      {
        key: 'strictMate',
        label: 'Forced mates: only the fastest mate counts',
        kind: 'toggle',
        help: 'When there is a forced mate, a move must keep a forced mate either way; with this on, it must also be as fast as the best one.',
      },
      { key: 'hints', label: 'Show the Hint button', kind: 'toggle' },
      { key: 'autoReplyDelay', label: "Opponent's reply delay", kind: 'range', min: 0, max: 2000, step: 100 },
    ],
  },
  {
    title: 'Playing on (live engine)',
    fields: [
      {
        key: 'live',
        label: 'Keep playing after the first move',
        kind: 'toggle',
        help: 'Until mate, a draw or a quiet position. Uses the add-on’s engines on desktop, a built-in Stockfish elsewhere.',
      },
      { key: 'goodMove', label: 'A move counts as good within', kind: 'range', min: 0.5, max: 10, step: 0.5, format: v => v + ' % win rate', when: c => c.live },
      { key: 'liveMaxMoves', label: 'At most', kind: 'range', min: 1, max: 20, step: 1, format: v => v + (v === 1 ? ' move' : ' moves'), when: c => c.live },
    ],
  },
  {
    title: 'Timed puzzles',
    fields: [
      { key: 'timed', label: 'Time limit on the puzzle', kind: 'toggle', help: 'The clock runs while you think, not during the opponent’s reply. The back shows the time taken.' },
      { key: 'timeLimit', label: 'Time to solve', kind: 'range', min: 5, max: 600, step: 5, format: v => v + ' s', when: c => c.timed },
      {
        key: 'timeAffects',
        label: 'When time is up',
        kind: 'select',
        options: [
          ['nothing', 'Just show the time'],
          ['fail', 'Fail the puzzle'],
          ['grade', 'Grade a slow solve lower'],
        ],
        when: c => c.timed,
      },
      { key: 'gradeSlow', label: 'Grade when solved too slowly', kind: 'select', options: EASES, when: c => c.timed && c.timeAffects === 'grade' && c.afterAnswer === 'grade', help: 'Needs “grade automatically” above; otherwise the back only says it was slow.' },
      { key: 'showClock', label: 'Show the clock', kind: 'toggle', help: 'Off: the time shows only on the back.', when: c => c.timed },
    ],
  },
  {
    title: 'Evaluation',
    fields: [
      { key: 'showEval', label: 'Show the engine evaluation', kind: 'toggle' },
      {
        key: 'evalOnPuzzleFront',
        label: 'Also at the puzzle position (gives hints away)',
        kind: 'toggle',
        when: c => c.showEval,
      },
    ],
  },
];

const NAG_ORDER = [3, 8, 1, 5, 6, 2, 4];

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function seconds(ms: number): string {
  return (ms / 1000).toFixed(1).replace(/\.0$/, '') + ' s';
}

export interface PanelOptions {
  /** Validated template config (synced defaults). */
  template: Config;
  /** Validated per-device overrides. */
  overrides: Partial<Config>;
  warnings: string[];
  storageOk: boolean;
  desktop: boolean;
  addon: Promise<boolean>;
  /** Called after the panel is removed; `changed` when the effective config changed. */
  onClose: (changed: boolean) => void;
}

export class SettingsPanel {
  readonly el: HTMLElement;
  private template: Config;
  private overrides: Partial<Config>;
  private changed = false;
  private addon = false;
  private message?: { text: string; bad?: boolean };
  private snippet?: string;
  private body!: HTMLElement;
  private foot!: HTMLElement;
  private closed = false;

  constructor(private readonly o: PanelOptions) {
    this.template = o.template;
    this.overrides = { ...o.overrides };
    this.el = h('div', 'kr-settings');
    this.el.setAttribute('role', 'dialog');
    this.el.setAttribute('aria-modal', 'true');
    this.el.setAttribute('aria-label', 'Card settings');
    const box = h('div', 'kr-settings-box');
    const head = h('div', 'kr-settings-head');
    head.appendChild(h('div', 'kr-settings-title', 'Card settings'));
    head.appendChild(this.button('kr-settings-x', '✕', () => this.close(), 'Close'));
    this.body = h('div', 'kr-settings-body');
    this.foot = h('div', 'kr-settings-foot');
    box.appendChild(head);
    box.appendChild(this.body);
    box.appendChild(this.foot);
    this.el.appendChild(box);
    this.el.addEventListener('click', e => {
      if (e.target === this.el) this.close();
    });
    this.render();
    o.addon.then(ok => {
      this.addon = ok;
      if (!this.closed) this.renderFoot();
    });
  }

  get config(): Config {
    return { ...this.template, ...this.overrides };
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.el.parentNode) this.el.parentNode.removeChild(this.el);
    this.o.onClose(this.changed);
  }

  focus(): void {
    const first = this.el.querySelector('select, input, button') as HTMLElement | null;
    if (first) first.focus();
  }

  // -------------------------------------------------------------------------

  private set<K extends keyof Config>(k: K, v: Config[K]): void {
    const cfg = this.config;
    cfg[k] = v;
    this.overrides = configDiff(cfg, this.template);
    if (!saveOverrides(this.overrides)) this.message = { text: 'Could not store the change on this device.', bad: true };
    else this.message = undefined;
    this.snippet = undefined;
    this.changed = true;
    this.render();
  }

  private reset(k?: keyof Config): void {
    if (k) {
      const o = { ...this.overrides } as Record<string, unknown>;
      delete o[k];
      this.overrides = o as Partial<Config>;
      saveOverrides(this.overrides);
    } else {
      this.overrides = {};
      clearOverrides();
    }
    this.message = undefined;
    this.snippet = undefined;
    this.changed = true;
    this.render();
  }

  private saveAll(): void {
    const cfg = this.config;
    this.message = { text: 'Saving…' };
    this.renderFoot();
    saveToTemplate(cfg).then(r => {
      if (this.closed) return;
      if (r.ok) {
        this.template = cfg;
        window.KR_CONFIG = { ...cfg };
        this.overrides = {};
        clearOverrides();
        this.changed = true;
        this.message = { text: 'Saved to the card template. It syncs to your other devices.' };
      } else this.message = { text: 'Could not save: ' + r.error, bad: true };
      this.render();
    });
  }

  private copy(): void {
    const text = formatConfig(this.config);
    this.snippet = text;
    this.message = undefined;
    this.render();
    const ta = this.foot.querySelector('textarea');
    const fallback = () => {
      if (!ta) return;
      ta.focus();
      ta.select();
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (_) {
        /* ignore */
      }
      this.note(ok ? 'Copied.' : 'Select the text below and copy it.');
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText)
        navigator.clipboard.writeText(text).then(() => this.note('Copied.'), fallback);
      else fallback();
    } catch (_) {
      fallback();
    }
  }

  private note(text: string): void {
    const n = this.foot.querySelector('.kr-settings-copied');
    if (n) n.textContent = text;
  }

  // -------------------------------------------------------------------------

  private button(cls: string, text: string, onClick: () => void, title?: string): HTMLButtonElement {
    const b = h('button', 'kr-btn ' + cls, text);
    b.type = 'button';
    if (title) {
      b.title = title;
      b.setAttribute('aria-label', title);
    }
    b.addEventListener('click', e => {
      e.preventDefault();
      e.stopPropagation();
      onClick();
    });
    return b;
  }

  private render(): void {
    const scroll = this.body.scrollTop;
    const focusedKey = (document.activeElement as HTMLElement | null)?.getAttribute?.('data-key');
    this.body.innerHTML = '';
    if (this.o.warnings.length) {
      const w = h('div', 'kr-settings-warn');
      w.appendChild(h('div', 'kr-settings-warn-title', '⚠ Some settings were ignored'));
      const ul = h('ul');
      this.o.warnings.forEach(t => ul.appendChild(h('li', undefined, t)));
      w.appendChild(ul);
      this.body.appendChild(w);
    }
    const cfg = this.config;
    GROUPS.forEach(g => {
      const sec = h('section', 'kr-settings-group');
      sec.appendChild(h('div', 'kr-settings-group-title', g.title));
      g.fields.forEach(f => {
        if (f.when && !f.when(cfg)) return;
        sec.appendChild(this.row(f, cfg));
      });
      this.body.appendChild(sec);
    });
    this.body.scrollTop = scroll;
    if (focusedKey) {
      const el = this.body.querySelector('[data-key="' + focusedKey + '"]') as HTMLElement | null;
      if (el) el.focus();
    }
    this.renderFoot();
  }

  private row(f: Field, cfg: Config): HTMLElement {
    const k = f.key;
    const overridden = Object.prototype.hasOwnProperty.call(this.overrides, k);
    const row = h('div', 'kr-settings-row kr-settings-' + f.kind + (overridden ? ' kr-overridden' : ''));
    const id = 'kr-set-' + k;
    const label = h('label', 'kr-settings-label');
    label.htmlFor = id;
    const text = h('span', undefined, f.label);
    label.appendChild(text);
    if (overridden) {
      const dot = h('span', 'kr-settings-dot');
      dot.title = 'Changed on this device';
      label.appendChild(dot);
    }

    const ctl = h('div', 'kr-settings-ctl');
    const v = cfg[k];
    if (f.kind === 'select') {
      const sel = h('select');
      sel.id = id;
      sel.setAttribute('data-key', k);
      f.options.forEach(([val, lab]) => {
        const op = h('option', undefined, lab);
        op.value = String(val);
        sel.appendChild(op);
      });
      sel.value = String(v);
      sel.addEventListener('change', () => {
        const raw = sel.value;
        this.set(k, (typeof DEFAULT_CONFIG[k] === 'number' ? Number(raw) : raw) as never);
      });
      ctl.appendChild(sel);
    } else if (f.kind === 'toggle') {
      const cb = h('input');
      cb.type = 'checkbox';
      cb.id = id;
      cb.setAttribute('data-key', k);
      cb.checked = !!v;
      cb.addEventListener('change', () => this.set(k, cb.checked as never));
      ctl.appendChild(cb);
    } else if (f.kind === 'range') {
      const r = h('input');
      r.type = 'range';
      r.id = id;
      r.setAttribute('data-key', k);
      r.min = String(f.min);
      r.max = String(f.max);
      r.step = String(f.step);
      r.value = String(v);
      const fmt = f.format || seconds;
      const out = h('span', 'kr-settings-value', fmt(v as number));
      r.addEventListener('input', () => (out.textContent = fmt(Number(r.value))));
      r.addEventListener('change', () => this.set(k, Number(r.value) as never));
      ctl.appendChild(r);
      ctl.appendChild(out);
    } else {
      const list = h('div', 'kr-settings-nags');
      const acc = v as number[];
      NAG_ORDER.forEach(n => {
        const info = NAGS[n];
        const lab = h('label', 'kr-settings-nag kr-nag-' + info.cls);
        const cb = h('input');
        cb.type = 'checkbox';
        cb.setAttribute('data-key', k + '-' + n);
        cb.checked = acc.indexOf(n) >= 0;
        cb.addEventListener('change', () => {
          const next = NAG_ORDER.filter(x => (x === n ? cb.checked : acc.indexOf(x) >= 0));
          this.set('acceptNags', next);
        });
        lab.appendChild(cb);
        lab.appendChild(h('span', 'kr-glyph', info.glyph));
        lab.appendChild(document.createTextNode(' ' + info.label));
        list.appendChild(lab);
      });
      ctl.appendChild(list);
    }

    row.appendChild(label);
    row.appendChild(ctl);
    if (overridden) row.appendChild(this.button('kr-settings-reset1', '↺', () => this.reset(k), 'Use the template value'));
    if (f.help) row.appendChild(h('div', 'kr-settings-help', f.help));
    return row;
  }

  private renderFoot(): void {
    const foot = this.foot;
    foot.innerHTML = '';
    const has = Object.keys(this.overrides).length > 0;
    let scope: string;
    if (this.addon)
      scope = has
        ? 'Changes apply on this device now. "Save for all devices" writes them into the card template so they sync everywhere.'
        : 'These are the card template settings, shared by all your devices.';
    else if (!this.o.storageOk)
      scope = "This device can't store settings, so changes last only for this card. Copy them into the card template to keep them.";
    else if (this.o.desktop)
      scope =
        'Saved on this device until Anki restarts. To keep them, install the Knightly Review add-on or copy them into the card template.';
    else scope = 'Saved on this device. To use them on all devices, copy them into the card template.';
    foot.appendChild(h('div', 'kr-settings-scope', scope));
    if (this.message)
      foot.appendChild(h('div', 'kr-settings-msg' + (this.message.bad ? ' kr-bad-text' : ''), this.message.text));

    const btns = h('div', 'kr-settings-btns');
    if (has) btns.appendChild(this.button('', 'Reset to template', () => this.reset()));
    btns.appendChild(this.button('', 'Copy as template config', () => this.copy()));
    if (this.addon && has) btns.appendChild(this.button('kr-primary', 'Save for all devices', () => this.saveAll()));
    btns.appendChild(this.button('kr-primary kr-settings-done', 'Done', () => this.close()));
    foot.appendChild(btns);

    if (this.snippet) {
      const wrap = h('div', 'kr-settings-snippet');
      wrap.appendChild(
        h(
          'div',
          'kr-settings-help',
          'Paste this over the window.KR_CONFIG block in the Front and Back templates (Tools → Manage Note Types → Knightly Review → Cards).',
        ),
      );
      const ta = h('textarea');
      ta.readOnly = true;
      ta.rows = CONFIG_KEYS.length + 2;
      ta.value = this.snippet;
      wrap.appendChild(ta);
      wrap.appendChild(h('div', 'kr-settings-copied'));
      foot.appendChild(wrap);
    }
  }
}


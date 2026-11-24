/* RsHub · interface sounds: the wiring. The engine is SH.sound in core.js (one AudioContext, the settings, the
   rules that keep it to one sound per gesture); this file connects the interface to it with a few delegated listeners,
   so modules only call SH.sound where a sound means something specific (a scan, a delete, an undo...).

   - click: a pointer (or controller) activation of a button, link, dock item, tab or row (a plain row only where its
     click does something: ROW_ACTS), unless something more specific
     already sounded for that same click (a toggle, a tab, the dock, a dropdown, a dialog, a delete...).
   - toggle_on / toggle_off: any .toggle or [role=switch]; tab_switch: a segmented control (.seg) changing its choice.
   - panel_open / panel_close: a dialog (the ones gamepad.js traps the focus in) opening or closing, after a click or a key:
     exactly one panel_close however it closes (its X or Cancel, a click on the backdrop, Esc, the controller's B). A
     dialog counts as closed the moment its fade-out starts. The palette and the settings sheet play their own (shell.js).
   - key_type: a printable key or Backspace typed in the command palette's search (not modifiers, not held-key repeats,
     not while an IME is composing), at most every 30 ms.
   - hover: entering a dock item, a button, a Home tile or a row that is a button, with a real mouse movement (never
     when the page scrolls under a still pointer), at most every 70 ms.
   - hover_soft: the same for everything else that can be clicked (tabs, segmented choices, switches, checkboxes, rows
     and cards that act, selects, links, sliders) and for the rows, list items and cards that light up under the
     pointer (ROWS), much quieter and at most every 90 ms; never for an element inside one that plays hover, nor right
     after a hover.
   - unavailable: pressing a disabled button.
   - app_start / admin_granted: once when the window first appears (not after a reload, not when it starts hidden).
   Preloading: the sounds a first click needs are fetched and decoded in idle time after the first paint. */
'use strict';
(() => {
const { $$, state } = SH;
const sound = SH.sound;

/* ---------- preload ---------- */
const COMMON = ['click', 'hover', 'hover_soft', 'tab_switch', 'navigate', 'toggle_on', 'toggle_off', 'panel_open', 'panel_close', 'dropdown',
  'palette_open', 'toast', 'success', 'error', 'warning', 'unavailable', 'app_exit', 'focus_move'];
const idle = fn => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 4000 }) : setTimeout(fn, 1200));
const soundsOn = () => state.settings.sounds !== false && +state.settings.soundVolume > 0;
/* the start sound is needed at once; the rest waits for the first paint and an idle moment */
SH.on('boot', () => {
  if (soundsOn()) sound.preload(['app_start']);
  requestAnimationFrame(() => idle(() => { if (soundsOn()) sound.preload(COMMON); }));
});
SH.on('setting', ({ key, val }) => { if (key === 'sounds' && val) idle(() => sound.preload(COMMON)); });

/* ---------- the window appears ---------- */
SH.on('shown', ({ shown }) => {
  if (!shown) return;  /* started hidden in the tray with Windows: no sound when it is opened later */
  let again = false; try { again = !!sessionStorage.getItem('sh.started'); sessionStorage.setItem('sh.started', '1'); } catch {}
  if (again) return;   /* a reload (language change): it already greeted */
  const s = state.settings, asked = +s.adminAsked || 0;
  if (asked) SH.setSetting('adminAsked', 0);
  /* back as administrator because the user asked for it a moment ago */
  if (state.admin && asked && Date.now() - asked < 180000) sound('admin_granted', { visible: true });
  else sound('app_start', { visible: true });
});

/* ---------- dialogs ---------- */
const MODALS = '.su-ov, .cs-o.on, .hg-dlg, .mdlg, .bk-dq-o, .dq-o, .u-confirm, .bug-pop, .ar-o';
const vis = el => el.checkVisibility ? el.checkVisibility({ checkVisibilityCSS: true }) : !!el.offsetParent;
/* every dialog closes with a short fade (on itself, or on its backdrop and box) before it is removed: from the moment
   that fade starts it is closed, so a click on its X, Cancel or backdrop sounds at once instead of a click */
const fadesOut = a => {
  if (a.playState !== 'running' || !a.effect || !a.effect.getKeyframes) return false;
  const k = a.effect.getKeyframes(); return k.length > 1 && k[k.length - 1].opacity !== undefined && +k[k.length - 1].opacity === 0;
};
const closing = el => [el, ...el.children].some(x => x.getAnimations && x.getAnimations().some(fadesOut));
const dialogs = () => $$(MODALS).filter(el => vis(el) && !closing(el));
let known = [];
/* +1 a dialog opened, -1 one closed, 0 nothing changed (the list is remembered for the next call) */
function dialogChange() {
  const now = dialogs(), opened = now.some(d => !known.includes(d)), closed = known.some(d => !now.includes(d) || !d.isConnected);
  known = now;
  return opened ? 1 : closed ? -1 : 0;
}
/* a dialog that opens or closes a moment after a click or a key (it waited for an answer, Esc closed it) */
let gestAt = 0, checks = [];
function afterGesture() {
  /* what is open right now, before the gesture acts: a dialog that opened by itself (the update dialog on a timer, one
     that came long after its click) would otherwise be "new" at the first press inside it (panel_open on its Later) and,
     closed with Esc or B, never have been open (no panel_close) */
  dialogChange();
  gestAt = performance.now(); checks.forEach(clearTimeout);
  checks = [0, 90, 420].map(ms => setTimeout(() => {
    const c = dialogChange(); if (!c) return;
    if (sound.last > gestAt) return;  /* that gesture already made its sound */
    sound(c > 0 ? 'panel_open' : 'panel_close');
  }, ms));
}
addEventListener('pointerdown', afterGesture, { capture: true, passive: true });
addEventListener('keydown', e => { if (e.isTrusted && (e.key === 'Escape' || e.key === 'Enter' || e.key === ' ')) afterGesture(); }, { capture: true, passive: true });
SH.on('boot', () => { if (SH.gamepad) SH.gamepad.onButton(afterGesture); });

/* ---------- rows that are not buttons ----------
   ROWS: the rows, list items and cards drawn as plain elements (a div, an svg group) that light up under the pointer
   (their :hover in the modules' CSS). They play hover_soft like any other secondary element. Loading placeholders and
   rows marked unavailable (.dis) stay quiet. tests/test_sounds.py checks that every :hover in the CSS is here, is a
   real control already covered by HOVER/SOFT, or is listed there as decorative.
   ROW_ACTS: the ones (or the part of one) whose click does something by itself (select, expand, open, mark): a click.
   A row that only shows things says nothing when clicked. */
const ROWS = [
  '.row', '.bk-row',
  '#v-monitor .pr', '#v-monitor .dv', '#v-monitor .evt',
  '#v-drivers .du-row', '#v-drivers .db-row', '#v-drivers .dh-row', '#v-drivers .ds-row',
  '#v-remover .qit', '#v-remover .he',
  '#v-updater .u-row', '#v-updater .u-card:not(:has(> .sk))', '#v-updater .u-pkr',
  '#v-hangar .hg-srow', '#v-hangar .hg-kit', '#v-hangar .hg-qrow',
  '#v-storage div.st-tr', '#v-storage .st-br', '#v-storage .st-dr',
  '#v-optimizer .pstep:not(.dis)', '#v-optimizer .cl-it', '#v-optimizer .cl-tr', '#v-optimizer .st-it', '#v-optimizer .sv-it',
  '#v-optimizer .db-it', '#v-optimizer .db-step', '#v-optimizer .sb',
  '#v-privacy .pv-f:not(.pv-pend)', '#v-privacy .pv-ps:not(.dis)', '#v-privacy .pv-ar', '#v-privacy .pv-port',
  '#v-privacy .pv-napp', '#v-privacy .pv-tg', '#v-privacy .pv-hi', '#v-privacy .pv-pit', '#v-privacy .pv-radar .blip',
  '#v-benchmark .bm-hr',
  '#v-tester .kb-stage.marking .k:not(.nd)',
  '#v-autopilot .ap-card', '#v-autopilot .si', '#v-autopilot .lg-it',
  'input[type=range]', 'input[type=color]',
].join(', ');
const ROW_ACTS = [
  '#v-drivers .du-row', '#v-drivers .ds-row',
  '#v-optimizer .cl-it:not(.clean, .busyrun) .cl-row', '#v-optimizer .cl-tr:not(.kept)', '#v-optimizer .st-it .st-r',
  '#v-optimizer .db-it:not(.keep, .rmv, .d, .done)',
  '#v-privacy .pv-napp', '#v-privacy .pv-pit .pv-pr0', '#v-privacy .pv-radar .blip',
  '#v-storage .st-tr[data-id]:not(.file)',
  '#v-tester .kb-stage.marking .k:not(.nd)',
  '#v-autopilot .ap-card',
].join(', ');

/* ---------- clicks ---------- */
const CLICKABLE = `button, a[href], [role=button], [role=tab], [role=option], [role=menuitem], [role=checkbox], .btn, .dk, .check, summary, label[for], ${ROW_ACTS}`;
const TOGGLE = '.toggle, [role=switch]';
/* what a click looked like before the page handled it (a click made by code inside another one has its own entry) */
const pre = new WeakMap();
const CHOICE = '.seg button, [role=tab]';
addEventListener('click', e => {
  const act = sound.begin();
  dialogChange();
  const el = e.target.closest && e.target.closest(`${TOGGLE}, ${CHOICE}, ${CLICKABLE}`);
  const wasOn = !!(el && el.matches(CHOICE) && (el.classList.contains('on') || el.getAttribute('aria-selected') === 'true'));
  pre.set(e, { act, el, wasOn, dis: !!(el && el.disabled) });
  /* this click will have the control's own sound (a switch, a new choice, a press with the pointer or the controller's
     A): a plain toast its handler shows stays quiet (core.js) */
  const pad = SH.sound.viaPad === true;
  if (el && !el.disabled && (e.isTrusted || pad) && (el.matches(TOGGLE) || (el.matches(CHOICE) && !wasOn) || e.detail > 0 || pad)) act.own = true;
}, true);
addEventListener('click', e => {
  const p = pre.get(e);
  if (!p || p.act.claimed || performance.now() - deadAt < 800) { dialogChange(); return; }
  /* real input only: a pointer, a key (Enter or Space on a button), or the controller's A. A click made by code is not. */
  const pad = SH.sound.viaPad === true, trusted = e.isTrusted || pad, pointer = e.detail > 0 || pad;
  if (!trusted) return;  /* (the check after the gesture still hears a dialog it opened or closed: the controller's B) */
  const el = p.el, c = dialogChange();
  if (p.dis) return;  /* disabled before the click (a button its own handler disables, as it starts, still clicks) */
  /* also a click on nothing clickable: the backdrop that closes a dialog */
  if (c > 0) { sound('panel_open'); return; }
  if (c < 0) { sound('panel_close'); return; }
  if (!el) return;
  if (el.matches(TOGGLE)) { const on = el.classList.contains('on') || el.getAttribute('aria-checked') === 'true'; sound(on ? 'toggle_on' : 'toggle_off'); return; }
  if (el.matches(CHOICE)) { if (!p.wasOn) sound('tab_switch'); return; }  /* the choice already made: nothing to say */
  if (pointer) sound('click');
});

/* ---------- a disabled button pressed ----------
   A disabled .btn has pointer-events:none (base.css), so the press lands on whatever is under it: look for one under
   the pointer, and keep that press's click silent. */
let deadAt = -1e9;
addEventListener('pointerdown', e => {
  if (e.button !== 0 || !e.isTrusted) return;
  const hit = el => { const r = el.getBoundingClientRect(); return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom; };
  const b = $$('button[disabled], .btn[disabled], [role=button][aria-disabled="true"]').find(el => !el.closest('.view:not(.is-on), [inert]') && hit(el) && vis(el));
  if (b) { deadAt = performance.now(); sound('unavailable'); }
}, { capture: true, passive: true });

/* ---------- typing in the command palette ---------- */
addEventListener('keydown', e => {
  const f = e.target; if (!f || f.id !== 'palIn' || !e.isTrusted) return;
  if (e.isComposing || e.keyCode === 229 || e.key === 'Process' || e.repeat) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === 'Backspace' ? !f.value : [...e.key].length !== 1) return;  /* Shift, arrows, Enter... say nothing */
  sound('key_type');
}, { passive: true });

/* ---------- hover ----------
   PRIMARY plays hover. SOFT is every other thing that can be clicked: it plays hover_soft, unless it sits inside a
   primary element (that one already spoke when the pointer came in) or a hover has just played. */
const HOVER = '.dk, .btn, .bt, button.row, .row[role=button], .row[tabindex="0"], .vcard, .tool, .lang';
const SOFT = `button, a[href], [role=button], [role=tab], [role=option], [role=menuitem], [role=switch], [role=checkbox], .toggle, .check, summary, label[for], select, .csel, [data-go], [tabindex="0"], #brand, ${ROWS}`;
const SOFT_GAP = 90;
let movedAt = -1e9, scrolledAt = -1e9, hoverAt = -1e9;
addEventListener('pointermove', e => { if (e.isTrusted && (e.movementX || e.movementY)) movedAt = performance.now(); }, { capture: true, passive: true });
addEventListener('scroll', () => { scrolledAt = performance.now(); }, { capture: true, passive: true });
addEventListener('wheel', () => { scrolledAt = performance.now(); }, { capture: true, passive: true });
document.addEventListener('pointerover', e => {
  if (e.pointerType !== 'mouse' || e.buttons || !e.isTrusted) return;
  const t = e.target; if (!t || !t.closest) return;
  const prim = t.closest(HOVER);
  const el = prim || t.closest(SOFT); if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return;
  if (e.relatedTarget && el.contains(e.relatedTarget)) return;   /* moving inside it, not entering */
  const now = performance.now();
  if (now - movedAt > 60 || now - scrolledAt < 250) return;      /* the page moved under a still pointer */
  if (document.documentElement.classList.contains('pad-mode')) return;
  if (prim) { if (sound('hover')) hoverAt = now; return; }
  if (now - hoverAt < SOFT_GAP) return;                           /* a primary one just spoke */
  sound('hover_soft');
}, { passive: true });

/* ---------- the loading loop ----------
   While a loading indicator is on screen (LOADING: a radar or a scan ring turning, an analysis loading, a list or a
   catalog standing in as skeletons), loading.ogg loops (SH.sound.loop, core.js). On screen: in the module in view,
   rendered, inside the window and its scroll boxes, and not covered (the palette, a dialog or the Settings sheet over
   its middle hides it). It starts once one has been there for 400 ms (a quick load stays silent), goes on while
   indicators come and go without a gap (a re-render, the radar giving way to the list's skeletons), and fades when
   none is left: the work ended or failed, the user went to another module or tab, scrolled it away, covered it, or
   the window was hidden. Not on the start screen (it covers everything). One loop however many indicators. Left out on
   purpose: spinners inside buttons and the progress bars of installs, cleanups, backups and tests (they have their
   own progress and sounds), the benchmark (it measures the PC) and the small skeletons of a single value. */
const LOADING = [
  '#v-drivers .du-radar.on', '#v-optimizer .ring.scanning', '#v-optimizer .opt-load', '#v-privacy .pv-radar.scanning',
  '#v-privacy .pv-scan.run', '#v-storage .st-scan.busy', '#v-remover .ck.run', '#v-updater.u-rescan .u-hero',
  '#v-updater .btn.u-busy', '#v-updater .u-dnote', '.sk-row', '.sk-app', '.sk-card', '.opt-sk', '.sk',
].join(', ');
const LOAD_SK = 100;     /* a bare .sk block counts from this height (a chart, a panel being read), not a line of text */
const LOAD_AFTER = 400, LOAD_EVERY = 200;
/* on screen: SH.onScreen (core.js), the same test count_tick and percent_step use */
function loading() {
  for (const el of $$(LOADING)) {
    if (el.matches('.sk') && el.getBoundingClientRect().height < LOAD_SK) continue;  /* (a skeleton row counts by itself) */
    if (SH.onScreen(el)) return true;
  }
  return false;
}
let loadSince = 0;
function loadCheck() {
  const st = state.settings, can = SH.booted && st.sounds !== false && st.soundTasks !== false && +st.soundVolume > 0 && !document.hidden;
  const now = performance.now(), on = can && loading();
  if (!on) { if (loadSince || sound.looping) sound.loop(false); loadSince = 0; }  /* gone: fade, and the next one may start it */
  else if (!loadSince) { loadSince = now; sound.preload(['loading']); }
  else if (now - loadSince >= LOAD_AFTER) sound.loop(true);
  setTimeout(loadCheck, LOAD_EVERY);
}
SH.on('boot', () => setTimeout(loadCheck, LOAD_EVERY));
})();

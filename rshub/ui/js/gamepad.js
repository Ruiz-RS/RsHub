/* RsHub · controller navigation: move around the whole app with a gamepad (built for the ROG Ally's Xbox-style pad).
   Standard Gamepad API, polled with requestAnimationFrame ONLY while a controller is connected, the window is visible
   and focused, and the setting is on. Without a controller nothing runs.

   Buttons (standard mapping):  D-pad / left stick move · A select (on a panel that scrolls: go inside it) · B back,
   close, or leave the panel · X the newest toast's action (View, Undo) · Y search · Menu (Start) settings
   View (Back) Home · LB / RB previous / next tab (settings section; inside a panel: a page up / down)
   LT / RT previous / next module · Right stick scrolls what the user is in (the panel, else the page), the focus stays.
   After a scroll (right stick, wheel, touch) the next move starts from what is on screen, never jumps back.

   Modules that need the raw buttons (the Device Tester) call SH.gamepad.capture(). See docs/UI_CONTRACT.md. */
'use strict';
(() => {
const { $, $$, state, anim, EASE, Spring, esc } = SH;
const t = SH.t;
const ROOT = document.documentElement;
const S = () => state.settings;
const enabled = () => S().gamepadNav !== false;
const isMin = () => S().intensity === 'min';

/* ---------- buttons ---------- */
const B = { A: 0, B: 1, X: 2, Y: 3, LB: 4, RB: 5, LT: 6, RT: 7, VIEW: 8, MENU: 9, LS: 10, RS: 11, UP: 12, DOWN: 13, LEFT: 14, RIGHT: 15, GUIDE: 16 };
const NAMES = Object.keys(B);
const DELAY = 380, REPEAT = 125, FAST = 70, STICK = .55, RDZ = .2;

/* ---------- glyphs: Xbox by default, PlayStation letters when the controller says it is one ---------- */
let layout = 'xbox';
const COL = { A: '#7FE0B4', B: '#FF6B7A', X: '#7FB8FF', Y: '#F4C26B' };
const PS = { A: ['✕', '#FFFFFF'], B: ['○', '#FF6B7A'], X: ['□', '#E39BD8'], Y: ['△', '#7FE0B4'], LB: 'L1', RB: 'R1', LT: 'L2', RT: 'R2' };
const svg = (w, inner) => `<svg class="pg" viewBox="0 0 ${w} 24" width="${w}" height="24" aria-hidden="true">${inner}</svg>`;
const pill = (txt, w = 34) => svg(w, `<rect x="1.5" y="4.5" width="${w - 3}" height="15" rx="7.5"/><text x="${w / 2}" y="15.6" text-anchor="middle">${txt}</text>`);
function glyph(name) {
  if (layout === 'ps' && PS[name]) {
    const p = PS[name];
    return Array.isArray(p) ? svg(24, `<circle cx="12" cy="12" r="10" style="--c:${p[1]}" class="face"/><text x="12" y="16.4" text-anchor="middle" style="fill:${p[1]};font-size:12px">${p[0]}</text>`) : pill(p);
  }
  switch (name) {
    case 'A': case 'B': case 'X': case 'Y': return svg(24, `<circle cx="12" cy="12" r="10" class="face" style="--c:${COL[name]}"/><text x="12" y="16.4" text-anchor="middle" style="fill:${COL[name]};font-size:12px">${name}</text>`);
    case 'LB': case 'RB': case 'LT': case 'RT': return pill(name);
    case 'VIEW': return svg(24, `<circle cx="12" cy="12" r="10"/><rect x="6.8" y="9.2" width="7.2" height="5.4" rx="1.2" class="ln"/><path d="M10 9.2V7.8h7.2v5.4H15.8" class="ln"/>`);
    case 'MENU': return svg(24, `<circle cx="12" cy="12" r="10"/><path d="M7.6 9h8.8M7.6 12h8.8M7.6 15h8.8" class="ln"/>`);
    case 'DPAD': return svg(24, `<path d="M9.5 2.5h5v7h7v5h-7v7h-5v-7h-7v-5h7z" class="ln"/>`);
    case 'LS': return svg(24, `<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4.2" class="dot"/><text x="12" y="-1" display="none">L</text>`);
    case 'RS': return svg(24, `<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4.2" class="dot"/>`);
  }
  return '';
}
const detectLayout = gp => { layout = /054c|dualsense|dualshock|playstation|sony/i.test(gp && gp.id || '') ? 'ps' : 'xbox'; };

/* ---------- state ---------- */
const pads = new Map();          /* index -> per-pad edge and repeat state */
const listeners = new Set();
let raf = 0, last = 0, mode = false, cur = null, capture = null, lastVal = 0, scopeRoot = null, hintKey = '', lx = NaN, ly = NaN;
const memo = new Map();          /* scope root -> the element that was focused there */
const viewMemo = new Map();      /* module -> the element last focused in it (coming back restores it) */
let sessionToast = false;

/* ---------- the focus ring (one element that glides between targets) and the hint bar ---------- */
const ring = document.createElement('div'); ring.id = 'padRing'; ring.setAttribute('aria-hidden', 'true');
ring.innerHTML = '<div class="pr-in"><div class="pr-fr"><b></b></div><i></i></div>';
const hints = document.createElement('div'); hints.id = 'padHints'; hints.setAttribute('role', 'note'); hints.setAttribute('aria-label', t('Controller buttons'));
document.body.append(ring, hints);
const rs = { x: new Spring(0, 420, 32), y: new Spring(0, 420, 32), w: new Spring(40, 420, 32), h: new Spring(40, 420, 32) };
let ringShown = false, ringSnap = true, lastRingStyle = '', frameN = 0;

/* ---------- helpers ---------- */
const TEXTY = new Set(['text', 'search', 'number', 'email', 'url', 'password', 'tel', 'date', 'time', 'datetime-local', 'month', 'week', '']);
const isTextField = el => !!el && ((el.tagName === 'INPUT' && TEXTY.has((el.getAttribute('type') || 'text').toLowerCase())) || el.tagName === 'TEXTAREA' || el.isContentEditable);
const editing = () => isTextField(document.activeElement);
const visible = el => el.checkVisibility ? el.checkVisibility({ checkVisibilityCSS: true }) : !!el.offsetParent;
const rectOf = el => el.getBoundingClientRect();
const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[tabindex]:not([tabindex="-1"]),[role=button],[role=tab],[role=option],[role=switch],[role=checkbox],[role=menuitem]';
const GUESS = 'div,li,tr,td,label,article,section,canvas,img,span,p,h3,h4';
const SKIP = '.winctl,.rz,#padRing,#padHints,#padPanel,#padTip,#boot,.tip,.csel-hidden';
const OVERLAYS = ['.csel-pop', '.su-ov', '.cs-o.on', '.sc-ov', '.hg-dlg', '.mdlg', '.bk-dq-o', '.dq-o', '.u-confirm', '.bug-pop', '.ar-o'];
const nativeEl = el => /^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY|LABEL)$/.test(el.tagName);

/* the topmost open overlay (dialog, sheet, palette, popup), or the whole page */
function scope() {
  if (state.palOpen) return $('#palO');
  if (state.setOpen && !$('#setO').classList.contains('closing')) {
    const over = topOverlay(); const sheet = $('#setO');
    if (over && (+getComputedStyle(over).zIndex || 0) > 60) return over;
    return sheet;
  }
  return topOverlay() || document.body;
}
function topOverlay() {
  let best = null, bz = -1;
  for (const sel of [...OVERLAYS, ...custom]) for (const o of $$(sel)) {
    if (!visible(o)) continue; const cs = getComputedStyle(o); if (cs.pointerEvents === 'none' && o.className.indexOf('sc-ov') < 0) continue;
    const z = (parseInt(cs.zIndex, 10) || 0) + (o.closest('.view') ? 0 : 100); if (z >= bz) { best = o; bz = z; }
  }
  return best;
}
const custom = [];

const viewEl = () => $('#v-' + state.view);
function usable(el, sc) {
  if (!el || !el.isConnected || !visible(el)) return false;
  if (sc && !sc.contains(el)) return false;
  const v = el.closest('.view'); if (v && v !== viewEl()) return false;
  const r = rectOf(el); return r.width > 2 && r.height > 2;
}

/* ---------- panels that scroll ("windows" inside the page) ----------
   The page itself scrolls in PAGE elements (a view in the compact layout, a tab, the settings body and its section
   list, the palette's results, a dropdown's list, a dialog's main body): the focus moves through them freely. Any
   other area that scrolls on its own (a long list, a text panel) is a panel: from the page it is ONE focus stop (so
   a 300-row list never stands between the user and the next panel), A enters it, the D-pad then moves among its
   items or scrolls it when it has none, LB / RB page through it and B leaves it. */
const PAGE = '.view,.tab,.mod,.tabs,.sheet,.sheet .body,.sheet nav,.pal,.pal .res,.csel-pop,.overlay,body,html';
const inside = [];               /* the panels entered, innermost last */
const OVER = 8;                  /* px of overflow before an area counts as scrolling */
const scrollsY = (el, cs) => el.scrollHeight - el.clientHeight > OVER && /(auto|scroll)/.test((cs || getComputedStyle(el)).overflowY);
function isPanel(el, sc) {
  if (!el || el.nodeType !== 1 || el.matches(PAGE) || !scrollsY(el)) return false;
  const r = rectOf(el); if (r.height < 44 || r.width < 90) return false;
  /* in a dialog (component sheet, confirmations, the update window...) its outermost scrolling area is its page */
  sc = sc || scope();
  if (sc !== document.body && sc.contains(el)) { for (let p = el.parentElement; p && p !== sc; p = p.parentElement) if (scrollsY(p)) return true; return false; }
  if (!el.closest('.view') && r.height > innerHeight * .8) return false;
  return true;
}
const insideRoot = () => inside[inside.length - 1] || null;

/* The part of el that can be seen: clipped by every ancestor that hides overflow without scrolling. Ancestors that
   scroll do not hide it (scrolling reveals it): their own box is what must be seen instead. */
function shownRect(el, stop) {
  const b = rectOf(el); let r = { l: b.left, t: b.top, r: b.right, b: b.bottom };
  for (let p = el.parentElement; p && p !== stop && p !== document.body; p = p.parentElement) {
    const cs = getComputedStyle(p);
    if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
    const pr = rectOf(p), sy = /(auto|scroll)/.test(cs.overflowY), sx = /(auto|scroll)/.test(cs.overflowX);
    if (sy || sx) { r = { l: sx ? pr.left : Math.max(r.l, pr.left), t: sy ? pr.top : Math.max(r.t, pr.top), r: sx ? pr.right : Math.min(r.r, pr.right), b: sy ? pr.bottom : Math.min(r.b, pr.bottom) }; continue; }
    r = { l: Math.max(r.l, pr.left), t: Math.max(r.t, pr.top), r: Math.min(r.r, pr.right), b: Math.min(r.b, pr.bottom) };
    if (r.r - r.l < 3 || r.b - r.t < 3) return null;
  }
  return r.r - r.l >= 3 && r.b - r.t >= 3 ? r : null;
}
/* a target with nothing to see or say: no text, no picture, no label, no fill or edge of its own */
function empty(el) {
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return false;
  if (el.textContent.trim() || el.getAttribute('aria-label') || el.title || el.querySelector('svg,img,canvas,video,input,select,textarea')) return false;
  const cs = getComputedStyle(el);
  return cs.backgroundColor === 'rgba(0, 0, 0, 0)' && cs.backgroundImage === 'none' && !(parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none') && cs.boxShadow === 'none';
}
const seeable = el => el.checkVisibility ? el.checkVisibility({ checkVisibilityCSS: true }) : visible(el);
/* see-through (opacity 0 on it or an ancestor): a closed popover (it also ignores the pointer) or a row button that
   shows on hover and focus. Only the second is a target: gamepad.css shows it while it has the focus. */
const faded = el => !!el.checkVisibility && !el.checkVisibility({ checkOpacity: true });

/* every focusable or clickable thing in the root (a scope or an entered panel); the items of a panel that scrolls
   are replaced by the panel itself, and a panel with no items (text) is a stop too */
function candidates(sc, inner) {
  const root = inner || sc;
  const out = []; const seen = new Set(); const view = viewEl(); const vh = innerHeight, vw = innerWidth, area = vw * vh;
  const ok = el => {
    if (seen.has(el) || el.disabled || el.getAttribute('aria-disabled') === 'true' || el.closest('[inert],[aria-hidden="true"],' + SKIP)) return false;
    const v = el.closest('.view'); if (v && v !== view) return false;
    const tb = el.closest('.tab'); if (tb && !tb.classList.contains('on')) return false;   /* a tab that is not shown */
    if (!seeable(el)) return false;
    const r = rectOf(el); if (r.width < 3 || r.height < 3) return false;
    /* see-through: only a small control a row shows on hover (an icon button) is a target, never a hidden bar */
    if (getComputedStyle(el).pointerEvents === 'none' || (faded(el) && !(el.matches(SEL) && r.width <= 48 && r.height <= 48))) return false;
    if (!inner && (r.bottom < -vh * 1.5 || r.top > vh * 2.5 || r.right < -vw || r.left > vw * 2)) return false;
    if (r.width * r.height > area * .45) return false;   /* a layout box, not a target */
    if (!shownRect(el, root) || empty(el)) return false;
    return true;
  };
  for (const el of $$(SEL, root)) { if (ok(el)) { seen.add(el); out.push(el); } }
  /* cards and rows with a click handler: found by their pointer cursor (the outermost one) */
  const cursor = new Map(); const cur_ = el => { let c = cursor.get(el); if (c === undefined) { c = getComputedStyle(el).cursor === 'pointer'; cursor.set(el, c); } return c; };
  for (const el of $$(GUESS, root)) {
    if (seen.has(el) || el.closest(SEL) && el.closest(SEL) !== el) continue;
    if (!cur_(el)) continue; const p = el.parentElement; if (p && p !== root && cur_(p)) continue;
    if (ok(el)) { seen.add(el); out.push(el); }
  }
  /* an outer element holding several others is a layout box, not a target */
  const items = out.filter(el => { let n = 0; for (const o of out) if (o !== el && el.contains(o) && ++n > 1) return false; return true; });
  /* panels: the outermost one under the root stands for everything inside it */
  const pc = new Map(); const panel = el => { let v = pc.get(el); if (v === undefined) { v = isPanel(el, sc); pc.set(el, v); } return v; };
  const stops = new Set(), res = [];
  for (const el of items) {
    let top = null; for (let p = el.parentElement; p && p !== root && p !== document.body; p = p.parentElement) if (panel(p)) top = p;
    if (!top) { res.push(el); continue; }
    if (!stops.has(top)) { stops.add(top); res.push(top); }
  }
  /* panels with nothing to press (text that overflows) are stops too, so they can be read */
  const zone = inner || (sc === document.body ? view : sc);
  if (zone) {
    const w = document.createTreeWalker(zone, NodeFilter.SHOW_ELEMENT);
    for (let el = w.nextNode(); el; el = w.nextNode()) {
      if (!el.clientHeight || el.scrollHeight - el.clientHeight <= OVER || stops.has(el)) continue;
      if (!panel(el) || !seeable(el) || el.closest('[inert],[aria-hidden="true"]')) continue;
      const v = el.closest('.view'); if (v && v !== view) continue;
      let nested = false; for (const s of stops) if (s.contains(el)) { nested = true; break; }
      if (nested || !shownRect(el, root)) continue;
      for (const s of [...stops]) if (el.contains(s)) { stops.delete(s); res.splice(res.indexOf(s), 1); }
      stops.add(el); res.push(el);
    }
  }
  /* an item that holds a panel stop is a box, not a target */
  return res.filter(el => stops.has(el) || ![...stops].some(s => el.contains(s)));
}

/* ---------- spatial navigation ---------- */
function best(from, list, dir) {
  const f = rectOf(from), fx = f.left + f.width / 2, fy = f.top + f.height / 2; let pick = null, ps = 1e9;
  for (const el of list) {
    if (el === from) continue; const r = rectOf(el), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    let along, alongC, gap, off;
    if (dir === 'right') { alongC = cx - fx; along = r.left - f.right; gap = Math.max(0, r.top - f.bottom, f.top - r.bottom); off = Math.abs(cy - fy); }
    else if (dir === 'left') { alongC = fx - cx; along = f.left - r.right; gap = Math.max(0, r.top - f.bottom, f.top - r.bottom); off = Math.abs(cy - fy); }
    else if (dir === 'down') { alongC = cy - fy; along = r.top - f.bottom; gap = Math.max(0, r.left - f.right, f.left - r.right); off = Math.abs(cx - fx); }
    else { alongC = fy - cy; along = f.top - r.bottom; gap = Math.max(0, r.left - f.right, f.left - r.right); off = Math.abs(cx - fx); }
    if (alongC <= 4) continue;
    const s = Math.max(0, along) + alongC * .15 + gap * 3 + off * .35;
    if (s < ps) { ps = s; pick = el; }
  }
  return pick;
}
function nearestTo(x, y, list) {
  let pick = null, ps = 1e12;
  for (const el of list) { const r = rectOf(el), d = (r.left + r.width / 2 - x) ** 2 + (r.top + r.height / 2 - y) ** 2; if (d < ps) { ps = d; pick = el; } }
  return pick;
}
function initialFor(sc) {
  if (sc.id === 'setO') { const n = $('#sheet nav .on'); if (n) return n; }
  if (sc.id === 'palO') { const n = $('.pal .it.on') || $('.pal .it'); if (n) return n; }
  if (sc === document.body) { const m = viewMemo.get(state.view); if (m && usable(m, sc) && candidates(sc).includes(m)) return m; }
  const list = candidates(sc);
  if (!list.length) return null;
  if (sc === document.body) {
    const v = viewEl(); const inView = list.filter(el => v && v.contains(el));
    const pool = inView.length ? inView : list;
    const tab = v && $('.subnav .on', v); if (state.view !== 'home' && tab && list.includes(tab)) return tab;
    return nearestTo(innerWidth / 2, innerHeight / 2, pool);
  }
  const act = sc.querySelector('.csel-o.on'); if (act && list.includes(act)) return act;
  return list.find(el => el.classList.contains('pri')) || nearestTo(innerWidth / 2, innerHeight / 2, list);
}

/* ---------- scrolling ---------- */
const scrollerOf = el => {
  for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
    const cs = getComputedStyle(p);
    if (/(auto|scroll)/.test(cs.overflowY) && p.scrollHeight > p.clientHeight + 2) return p;
  }
  return null;
};
/* the page's own scroller: the view in the compact layout, else the open tab when it scrolls */
function pageScroller() {
  const sc = scope();
  if (sc !== document.body) { const b = $('.sheet .body', sc) || $('.pal .res', sc); return b && b.scrollHeight > b.clientHeight + 2 ? b : scrollerOf(cur && sc.contains(cur) ? cur : sc); }
  const v = viewEl(); if (!v) return null;
  if (v.scrollHeight > v.clientHeight + 2 && /(auto|scroll)/.test(getComputedStyle(v).overflowY)) return v;
  const t = $('.tab.on', v); return t && t.scrollHeight > t.clientHeight + 2 && /(auto|scroll)/.test(getComputedStyle(t).overflowY) ? t : null;
}
/* the dock and the hint bar float over the bottom of the page: what is behind them is not "in view" */
function floorY() {
  if (scope() !== document.body) return innerHeight;
  let y = innerHeight; const d = $('#dock'); if (d) { const r = rectOf(d); if (r.height) y = Math.min(y, r.top); }
  if (hints.classList.contains('on')) { const r = rectOf(hints); if (r.height) y = Math.min(y, r.top); }
  return y;
}
/* the bottom of the top bar: page content scrolled above it is not on screen */
function ceilY() { if (scope() !== document.body) return 0; const t = $('.top'); return t ? rectOf(t).bottom : 0; }
/* the part of el that is on screen now (every scroller and the floor included), or null */
const FLOATING = '#dock,#toasts,.top,#padHints,.ver-tag';
function onScreen(el) {
  const b = rectOf(el); let r = { l: b.left, t: b.top, r: b.right, b: b.bottom };
  for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
    const cs = getComputedStyle(p); if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
    const pr = rectOf(p); r = { l: Math.max(r.l, pr.left), t: Math.max(r.t, pr.top), r: Math.min(r.r, pr.right), b: Math.min(r.b, pr.bottom) };
  }
  /* the dock and the hint bar float over the page: content behind them is not on screen (they themselves are) */
  if (!el.closest(FLOATING)) { r.b = Math.min(r.b, floorY()); r.t = Math.max(r.t, ceilY()); }
  r.t = Math.max(r.t, 0); r.r = Math.min(r.r, innerWidth); r.l = Math.max(r.l, 0);
  return r.r - r.l >= 3 && r.b - r.t >= 3 ? r : null;
}
/* in view: enough of it shows to see what it is (half of it, or 24 px of a tall one) */
function inView(el) {
  const r = onScreen(el); if (!r) return false;
  const h = rectOf(el).height; return r.b - r.t >= Math.min(24, h * .5) - .5;
}
function reveal(el, instant) {
  const M = 26; const smooth = instant || isMin() ? 'instant' : 'smooth'; const floor = floorY();
  for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    const cs = getComputedStyle(p); const sy = /(auto|scroll)/.test(cs.overflowY) && p.scrollHeight > p.clientHeight + 2, sx = /(auto|scroll)/.test(cs.overflowX) && p.scrollWidth > p.clientWidth + 2;
    if (!sy && !sx) continue;
    const r = rectOf(el), c0 = rectOf(p), c = { top: c0.top, bottom: Math.min(c0.bottom, floor), left: c0.left, right: c0.right };
    c.height = c.bottom - c.top; let dy = 0, dx = 0;
    if (sy) { if (r.top < c.top + M) dy = r.top - c.top - M; else if (r.bottom > c.bottom - M) dy = r.bottom - c.bottom + M; if (r.height > c.height - 2 * M && r.top < c.top + M) dy = r.top - c.top - M; if (r.height > c.height - 2 * M && dy > 0) dy = r.top - c.top - M; }
    if (sx) { if (r.left < c.left + M) dx = r.left - c.left - M; else if (r.right > c.right - M) dx = r.right - c.right + M; }
    if (dx || dy) p.scrollBy({ top: dy, left: dx, behavior: smooth });
  }
}
/* scroll an area by a part of its height; false when it is already at that end */
function scrollStep(p, d, part = .4, instant) {
  if (!p) return false;
  const max = p.scrollHeight - p.clientHeight; if ((d > 0 && p.scrollTop >= max - 1) || (d < 0 && p.scrollTop <= 1)) return false;
  p.scrollBy({ top: d * Math.max(40, p.clientHeight * part), behavior: instant || isMin() ? 'instant' : 'smooth' }); frameN = 0; return true;
}

/* ---------- focus ---------- */
function setCur(el, snap) {
  if (!el) return; cur = el;
  const a = document.activeElement;
  if (isTextField(el)) { if (isTextField(a) && a !== el) a.blur(); }
  else {
    if (isTextField(a)) a.blur();
    /* a panel stop takes the focus too (keyboard parity: Page Up / Down then scroll it) */
    if (isPanel(el) && !el.hasAttribute('tabindex')) { el.tabIndex = -1; el.dataset.padStop = '1'; }
    try { el.focus({ preventScroll: true }); } catch {}
  }
  reveal(el); aimRing(snap);
  scrollerCache = null; hkAt = 0;
  if (scope() === document.body && !insideRoot()) viewMemo.set(state.view, el);
  paintTip(); hintKey = ''; if (mode) paintHints();
}
function aimRing(snap) {
  if (!cur || !ring) return;
  const r = rectOf(cur); rs.x.t = r.left; rs.y.t = r.top; rs.w.t = r.width; rs.h.t = r.height;
  ring.style.setProperty('--rr', getComputedStyle(cur).borderTopLeftRadius || '12px');
  if (snap || !ringShown) { rs.x.set(r.left); rs.y.set(r.top); rs.w.set(r.width); rs.h.set(r.height); ringSnap = true; }
  frameN = 0;
}
function pulse() { const i = $('.pr-in i', ring); if (i && !isMin()) anim(i, [{ transform: 'scale(1)', opacity: .9 }, { transform: 'scale(1.1)', opacity: 0 }], { d: 420, e: EASE.out }); const p = $('.pr-in', ring); if (p) anim(p, [{ transform: 'scale(1)' }, { transform: 'scale(.95)', offset: .3 }, { transform: 'scale(1)' }], { d: 300, e: EASE.spring }); }
function bump(dir) {
  const p = $('.pr-in', ring); if (!p || isMin()) return; const dx = dir === 'left' ? -1 : dir === 'right' ? 1 : 0, dy = dir === 'up' ? -1 : dir === 'down' ? 1 : 0;
  anim(p, [{ transform: 'translate(0,0)' }, { transform: `translate(${dx * 6}px,${dy * 6}px)`, offset: .4 }, { transform: 'translate(0,0)' }], { d: 260, e: EASE.spring });
}
function move(dir) {
  if (editing()) { if (dir !== 'up' && dir !== 'down') return; const a = document.activeElement; cur = a; a.blur(); }
  const sc = scope(), inner = insideRoot(), root = inner || sc; const list = candidates(sc, inner);
  if (cur && cur.tagName === 'INPUT' && cur.type === 'range' && (dir === 'left' || dir === 'right') && usable(cur, root)) { stepRange(cur, dir === 'right' ? 1 : -1); return; }
  /* a panel entered that has nothing to press: the D-pad reads it (scrolls it) */
  if (inner && cur === inner) {
    const n = list.length && (dir === 'down' || dir === 'right') ? firstVisible(list, inner) : null;
    if (n) { setCur(n); SH.sound('focus_move'); return; }
    if (dir === 'up' || dir === 'down') { if (!scrollStep(inner, dir === 'down' ? 1 : -1)) bump(dir); } else bump(dir);
    return;
  }
  if (!usable(cur, root) || !list.includes(cur)) {
    const m = cur && cur.isConnected && list.find(el => el !== cur && el.contains(cur));   /* its panel became a stop */
    const n = m || (lastRect ? nearestTo(lastRect.x, lastRect.y, list.filter(inView)) : null) || (inner ? firstVisible(list, inner) : initialFor(sc));
    n && setCur(n, true); return;
  }
  /* the focus was scrolled out of sight (right stick, wheel, touch): carry on from what is on screen instead of
     jumping back to it */
  if (!inView(cur)) {
    const vis = list.filter(el => el !== cur && inView(el));
    const n = best(cur, vis, dir) || nearestTo(...viewCentre(root), vis);
    if (n) { setCur(n); SH.sound('focus_move'); return; }
  }
  let n = best(cur, list, dir);
  /* the dock floats over the page: going down, the page's own content comes first (scrolled into view), the dock
     only once nothing is left below */
  if (n && dir === 'down' && !inner && n.closest('#dock') && !cur.closest('#dock')) { const m = best(cur, list.filter(el => !el.closest(FLOATING)), dir); if (m && rectOf(m).bottom > floorY() - 2) n = m; }
  /* the same going up past the top bar: the page's tabs and header (scrolled under it) come first */
  if (n && dir === 'up' && !inner && n.closest('.top') && !cur.closest(FLOATING)) { const m = best(cur, list.filter(el => !el.closest(FLOATING)), dir); if (m && rectOf(m).top < ceilY() + 2) n = m; }
  if (n) { setCur(n); SH.sound('focus_move'); return; }
  /* nothing further that way: scroll what is left to read of the panel or the page, then bump */
  if (dir === 'up' || dir === 'down') { const d = dir === 'down' ? 1 : -1; if (scrollStep(inner || pageScroller(), d)) return; }
  bump(dir);
}
function viewCentre(root) {
  const r = root && root !== document.body ? rectOf(root) : { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
  return [(r.left + r.right) / 2, (Math.max(0, r.top) + Math.min(floorY(), r.bottom)) / 2];
}
/* the first item on screen in a panel (top left), else its first item */
function firstVisible(list, root) {
  const vis = list.filter(inView); const pool = vis.length ? vis : list; const r = rectOf(root);
  return nearestTo(r.left, r.top, pool);
}
/* ---------- entering and leaving a panel ---------- */
function enter(p) {
  inside.push(p); const list = candidates(scope(), p);
  const n = list.length ? firstVisible(list, p) : null;
  if (n) setCur(n); else { cur = p; aimRing(); }
  scrollerCache = null;  /* the right stick scrolls the panel now, not what it scrolled before */
  paintPanel(); hintKey = ''; paintHints(); SH.sound('focus_move');
}
function leave() {
  const p = inside.pop(); paintPanel(); scrollerCache = null;
  if (p && p.isConnected) setCur(p); hintKey = ''; paintHints(); bump('up');
}
/* LB / RB inside a panel: a page up or down, the focus on the first item then on screen */
function page(d) {
  const p = insideRoot(); if (!p) return false;
  if (!scrollStep(p, d, .85, true)) { bump(d < 0 ? 'up' : 'down'); return true; }
  const list = candidates(scope(), p), top = rectOf(p).top - 1;
  if (list.length) { const vis = list.filter(el => inView(el) && rectOf(el).top >= top); const n = vis.length ? nearestTo(rectOf(p).left, top, vis) : firstVisible(list, p); if (n) setCur(n); } else aimRing();
  SH.sound('focus_move'); return true;
}
function stepRange(el, d) {
  const step = +el.step || 1, min = +el.min || 0, max = el.max === '' ? 100 : +el.max;
  const v = Math.min(max, Math.max(min, +el.value + d * step * Math.max(1, Math.round((max - min) / step / 20))));
  if (v === +el.value) { bump(d > 0 ? 'right' : 'left'); return; }
  el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); aimRing();
}

/* ---------- actions ---------- */
/* viaPad: js/sounds.js treats this click like a pointer's (a click sound, a toggle's sound...) */
function click(el) {
  SH.sound.viaPad = true;
  try {
    if (nativeEl(el)) { el.click(); return; }
    const r = rectOf(el), o = { bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0, buttons: 1, pointerType: 'mouse', isPrimary: true, view: window };
    el.dispatchEvent(new PointerEvent('pointerdown', o)); el.dispatchEvent(new MouseEvent('mousedown', o));
    el.dispatchEvent(new PointerEvent('pointerup', { ...o, buttons: 0 })); el.dispatchEvent(new MouseEvent('mouseup', { ...o, buttons: 0 })); el.dispatchEvent(new MouseEvent('click', { ...o, buttons: 0 }));
  } finally { SH.sound.viaPad = false; }
}
function activate() {
  if (editing()) { const a = document.activeElement; if (a.tagName !== 'TEXTAREA') a.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true, cancelable: true })); return; }
  const sc = scope(), inner = insideRoot(); let el = cur;
  if (inner && el === inner) { bump('down'); return; }   /* inside a panel with nothing to press */
  if (!usable(el, inner || sc)) { const n = initialFor(sc); if (n) setCur(n, true); else if (sc !== document.body) click(sc); return; }
  pulse();
  if (!nativeEl(el) && isPanel(el)) { enter(el); return; }   /* a panel that scrolls: go inside it */
  if (isTextField(el)) { const was = document.activeElement === el; el.focus(); try { el.select(); } catch {} aimRing(); if (!was) touchKeyboard(); hintKey = ''; paintHints(); return; }
  if (el.tagName === 'SELECT') { el.focus(); try { el.showPicker && el.showPicker(); } catch {} return; }
  if (el.tagName === 'INPUT' && el.type === 'range') return;
  click(el); sync(90); sync(420);
}
const key = (k, o = {}) => window.dispatchEvent(new KeyboardEvent('keydown', { key: k, code: k === ',' ? 'Comma' : k === 'Escape' ? 'Escape' : 'Key' + k.toUpperCase(), bubbles: true, cancelable: true, ...o }));
function back() {
  if (editing()) { const a = document.activeElement; a.blur(); cur = a; aimRing(); hintKey = ''; paintHints(); return; }
  if (insideRoot()) { leave(); return; }
  if (state.palOpen || state.setOpen) { const over = scope(); if (over.id !== 'palO' && over.id !== 'setO') closeOverlay(over); else key('Escape'); sync(120); return; }
  const over = topOverlay(); if (over) { closeOverlay(over); sync(120); return; }
  const note = $('#adminNote'); if (note && note.classList.contains('on')) { $('#adminNo').click(); return; }
  if (state.view === 'home') { bump('left'); return; }
  const v = state.view, tab = $('#v-' + v + ' .subnav .on'), tb = tab && tab.dataset.tab;
  SH.back(); const t2 = $('#v-' + state.view + ' .subnav .on');
  if (state.view === v && (t2 ? t2.dataset.tab : null) === (tb || null)) SH.go('home');
  sync(120);
}
function closeOverlay(o) {
  if (o.matches('.cs-o') && SH.closeComponent) { SH.closeComponent(); return; }
  const x = $('[data-close],[data-dismiss],.x,[aria-label="Close"]', o); if (x && visible(x)) { x.click(); return; }
  const a = document.activeElement && document.activeElement !== document.body ? document.activeElement : o;
  a.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true, cancelable: true }));
}
function tab(d) {
  if (page(d)) return;
  if (state.palOpen) return;
  let list, sel;
  if (state.setOpen) { list = $$('#sheet nav [data-sec]'); sel = 'on'; }
  else if (topOverlay()) return;
  else { const nav = $('#v-' + state.view + ' .subnav'); if (!nav) { bump(d < 0 ? 'left' : 'right'); return; } list = $$('[data-tab]', nav); }
  const i = list.findIndex(b => b.classList.contains('on')), j = i + d;
  if (i < 0 || j < 0 || j >= list.length) { bump(d < 0 ? 'left' : 'right'); return; }
  list[j].click(); pulse(); sync(60); setCur(list[j]);
}
function module(d) {
  if (state.palOpen || state.setOpen || topOverlay()) return;
  const ids = $$('#dock .dk').map(b => b.dataset.go), i = ids.indexOf(state.view), j = i + d;
  if (i < 0 || j < 0 || j >= ids.length) { bump(d < 0 ? 'left' : 'right'); return; }
  SH.go(ids[j]); setTimeout(() => { if (!mode) return; const n = initialFor(document.body); n && setCur(n, true); }, 60);
}
const home = () => { if (state.palOpen || state.setOpen) key('Escape'); const o = topOverlay(); if (o) closeOverlay(o); SH.go('home'); setTimeout(() => { if (mode) { const n = initialFor(scope()); n && setCur(n, true); } }, 80); };
const settings = () => { key(',', { ctrlKey: true }); sync(80); sync(400); };
const palette = () => { key('k', { ctrlKey: true }); sync(80); sync(400); };

function press(i) {
  SH.sound.gesture();  /* interface sounds may follow a controller press, as they follow a click or a key */
  switch (i) {
    case B.A: activate(); break;
    case B.B: back(); break;
    case B.X: toastAction(); break;
    case B.Y: palette(); break;
    case B.LB: tab(-1); break;
    case B.RB: tab(1); break;
    case B.LT: module(-1); break;
    case B.RT: module(1); break;
    case B.VIEW: home(); break;
    case B.MENU: settings(); break;
  }
}

/* ---------- the hint bar ---------- */
/* what A does on the focused element */
function aLabel(el) {
  if (!el) return t('Select');
  if (isPanel(el) && !nativeEl(el)) return t('Enter');
  if (isTextField(el)) return t('Type text');
  if (el.matches('.csel,select,[aria-haspopup]')) return t('Open');
  return t('Select');
}
function hintItems() {
  const sc = scope(), inner = insideRoot();
  if (editing()) return [['A', t('Type text')], ['B', t('Done')]];
  const range = cur && cur.tagName === 'INPUT' && cur.type === 'range' ? [['DPAD', t('Adjust')]] : [];
  const tb = toastBtn(); const x = tb ? [['X', tb.textContent.trim()]] : [];
  if (inner) {
    const reading = cur === inner;
    return [...(reading ? [['DPAD', t('Scroll')]] : [['A', aLabel(cur)], ...range]), ['B', t('Leave')], ['LB RB', t('Pages')], ['RS', t('Scroll')], ...x];
  }
  if (state.setOpen && sc.id === 'setO') return [['A', aLabel(cur)], ...range, ['B', t('Close')], ['LB RB', t('Sections')], ['MENU', t('Close')]];
  if (state.palOpen) return [['A', t('Open')], ['B', t('Close')], ['Y', t('Close')]];
  if (sc !== document.body) return [['A', aLabel(cur)], ...range, ['B', t('Close')]];
  const tabs = $('#v-' + state.view + ' .subnav'), hk = hooked();
  if (hk) return [['A', aLabel(cur)], ...range, ...hk.hints(), ['B', t('Back')], ...x, ['LT RT', t('Modules')], ['Y', t('Search')], ['MENU', t('Settings')]];
  return [['A', aLabel(cur)], ...range, ['B', t('Back')], ...x, ...(tabs ? [['LB RB', t('Tabs')]] : []), ['LT RT', t('Modules')], ['Y', t('Search')], ['MENU', t('Settings')], ['VIEW', t('Home')]];
}

/* ---------- the panel the user is inside: a calm frame around it (the ring stays on the item) ---------- */
const panelFrame = document.createElement('div'); panelFrame.id = 'padPanel'; panelFrame.setAttribute('aria-hidden', 'true'); document.body.append(panelFrame);
function paintPanel() {
  const p = insideRoot(); panelFrame.classList.toggle('on', !!p && mode && !capture);
  ROOT.classList.toggle('pad-inside', !!p && mode);
  if (p) placeFrame();
}
function placeFrame() {
  const p = insideRoot(); if (!p || !p.isConnected) return;
  const r = rectOf(p); const k = `${r.left.toFixed(0)},${r.top.toFixed(0)},${r.width.toFixed(0)},${r.height.toFixed(0)}`;
  if (panelFrame.dataset.k === k) return; panelFrame.dataset.k = k;
  panelFrame.style.transform = `translate3d(${r.left}px,${r.top}px,0)`; panelFrame.style.width = r.width + 'px'; panelFrame.style.height = r.height + 'px';
  panelFrame.style.setProperty('--rr', getComputedStyle(p).borderTopLeftRadius || '14px');
}

/* ---------- the name of an icon-only target, under the ring (the tooltip a mouse would get) ---------- */
const tip = document.createElement('div'); tip.id = 'padTip'; tip.setAttribute('aria-hidden', 'true'); document.body.append(tip);
function paintTip() {
  const el = cur; let txt = '';
  if (mode && !capture && el && el.isConnected && !el.textContent.trim()) txt = el.getAttribute('aria-label') || el.title || '';
  if (tip.textContent !== txt) tip.textContent = txt;
  tip.classList.toggle('on', !!txt);
}
function placeTip() {
  if (!tip.classList.contains('on') || !cur) return;
  const r = rectOf(cur), w = tip.offsetWidth, h = tip.offsetHeight;
  const x = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)); let y = r.bottom + 12;
  if (y + h > floorY() - 6) y = r.top - h - 12;
  tip.style.transform = `translate3d(${Math.round(x)}px,${Math.round(y)}px,0)`;
}

/* ---------- X: the action of the newest toast (View, Undo...) ---------- */
function toastBtn() { const l = $$('#toasts .toast:not([data-dead]) .btn'); return l.length ? l[l.length - 1] : null; }
function toastAction() { const b = toastBtn(); if (b) { pulse(); click(b); sync(120); } else bump('down'); }

/* ---------- the touch keyboard: a controller has no keys, so a text field asks Windows for its own ---------- */
function touchKeyboard() { if (mode && SH.call) Promise.resolve(SH.call('touch_keyboard')).catch(() => {}); }
function paintHints() {
  hints.classList.toggle('in-sheet', state.setOpen && scope().id === 'setO');
  const items = hintItems(), k = layout + items.map(x => x.join(':')).join('|'); if (k === hintKey) return; hintKey = k;
  hints.innerHTML = items.map(([g, l]) => `<span class="ph">${g.split(' ').map(glyph).join('')}<b>${esc(l)}</b></span>`).join('');
}

/* The hint bar never covers content: while it is up the page reserves room for it (html.pad-hints raises
   --dockspace, see gamepad.css) and the views are told to re-fit. */
function showHints(on) {
  hints.classList.toggle('on', on);
  if (ROOT.classList.contains('pad-hints') === on) return;
  ROOT.classList.toggle('pad-hints', on);
  requestAnimationFrame(() => dispatchEvent(new Event('resize')));
}

/* ---------- mode: the ring and the hints only exist while a controller is being used ---------- */
function setMode(on) {
  if (mode === on) return; mode = on;
  ROOT.classList.toggle('pad-mode', on); ring.classList.toggle('on', on && !capture); showHints(on && !capture);
  if (on) {
    hintKey = ''; paintHints();
    if (!isMin()) anim(hints, [{ opacity: 0, transform: 'translate3d(-50%,10px,0) scale(.96)' }, { opacity: 1, transform: 'translate3d(-50%,0,0) scale(1)' }], { d: 420, e: EASE.spring });
    const sc = scope(); scopeRoot = sc; if (!usable(cur, sc)) { const n = initialFor(sc); if (n) setCur(n, true); } else aimRing(true);
    ringShown = true; ringSnap = true;
    if (!isMin() && cur) anim($('.pr-in', ring), [{ transform: 'scale(1.18)', opacity: 0 }, { transform: 'scale(1)', opacity: 1 }], { d: 460, e: EASE.spring });
  } else ringShown = false;
  paintPanel(); paintTip();
  SH.emit && SH.emit('gamepad:mode', on);
}
function sync(ms) { setTimeout(() => { if (mode && !capture) validate(true); }, ms || 0); }
let lastView = '';
function validate(force) {
  const now = performance.now(); if (!force && now - lastVal < 140) return; lastVal = now;
  const sc = scope();
  /* a panel that went away (closed, re-rendered, no longer scrolls) or another overlay on top: back to the page */
  while (inside.length && (!usable(insideRoot(), sc) || !scrollsY(insideRoot()))) { const p = inside.pop(); scrollerCache = null; if (cur && p.contains(cur)) cur = p.isConnected ? p : null; }
  if (sc !== scopeRoot || state.view !== lastView) {
    if (sc !== scopeRoot) inside.length = 0;
    /* the focus each scope had, for coming back to it: the page, or a dialog that another one opened on top of (Settings
       under the update dialog); a dialog that closed forgets it, so opening it again starts at its first control */
    if (scopeRoot && scopeRoot !== document.body && !visible(scopeRoot)) memo.delete(scopeRoot);
    else if (scopeRoot && cur && cur.isConnected) memo.set(scopeRoot, cur);
    /* another module: its own last focus (initialFor), not the one this scope had */
    const viewChanged = state.view !== lastView && sc === document.body && scopeRoot === sc; scopeRoot = sc; lastView = state.view;
    if (viewChanged) { inside.length = 0; const n = initialFor(sc); if (n) setCur(n, true); else cur = null; }
    else { const m = memo.get(sc); const n = usable(m, sc) ? m : initialFor(sc); if (n) setCur(n, true); else cur = null; }
  } else if (!editing() && !usable(cur, insideRoot() || sc)) {
    const r = lastRect, n = candidates(sc, insideRoot()); const p = r ? nearestTo(r.x, r.y, n) : initialFor(sc); if (p) setCur(p, true); else cur = null;
  }
  paintPanel(); paintTip();
  paintHints();
}
let lastRect = null;

/* ---------- the loop ---------- */
const shouldRun = () => enabled() && pads.size > 0 && !document.hidden && document.hasFocus();
function kick() {
  if (shouldRun()) { if (!raf) { last = performance.now(); raf = requestAnimationFrame(frame); } }
  else { if (raf) { cancelAnimationFrame(raf); raf = 0; } for (const p of pads.values()) p.primed = false; if (mode) setMode(false); }
}
function frame(ts) {
  raf = 0; if (!shouldRun()) { kick(); return; }
  const dt = Math.min(.05, (ts - last) / 1000); last = ts;
  const list = navigator.getGamepads ? navigator.getGamepads() : []; let any = false;
  for (const gp of list) { if (!gp || !gp.connected) continue; any = true; if (!pads.has(gp.index)) pads.set(gp.index, { btn: [], primed: false, dir: null, next: 0, since: 0, chord: 0 }); poll(gp, ts, dt); }
  if (!any) { pads.clear(); kick(); return; }
  if (mode && !capture) { validate(); tickRing(dt); }
  raf = requestAnimationFrame(frame);
}
function readDir(gp) {
  const b = gp.buttons, dn = i => !!b[i] && (b[i].pressed || b[i].value > .5);
  if (dn(B.UP)) return 'up'; if (dn(B.DOWN)) return 'down'; if (dn(B.LEFT)) return 'left'; if (dn(B.RIGHT)) return 'right';
  const x = gp.axes[0] || 0, y = gp.axes[1] || 0; if (Math.hypot(x, y) < STICK) return null;
  return Math.abs(x) > Math.abs(y) ? (x > 0 ? 'right' : 'left') : (y > 0 ? 'down' : 'up');
}
function poll(gp, ts, dt) {
  const st = pads.get(gp.index); const bs = [];
  for (let i = 0; i <= 16; i++) { const b = gp.buttons[i]; bs[i] = !!b && (b.pressed || b.value > .5); }
  if (!st.primed) { st.btn = bs; st.primed = true; detectLayout(gp); return; }
  const edges = []; for (let i = 0; i <= 16; i++) if (bs[i] && !st.btn[i]) edges.push(i);
  const dir = readDir(gp), rx = gp.axes[2] || 0, ry = gp.axes[3] || 0; st.btn = bs;
  if (edges.length || dir || Math.hypot(rx, ry) > STICK) SH.poke && SH.poke();  /* using the pad counts as activity (the idle calm in shell.js) */
  if (listeners.size) for (const i of edges) for (const fn of listeners) { try { fn({ index: i, name: NAMES[i], pad: gp }); } catch (e) { console.error(e); } }
  if (capture) {
    /* the test owns the buttons; View + Menu held for a second gives them back */
    if (bs[B.VIEW] && bs[B.MENU]) { st.chord += dt; capture.onProgress && capture.onProgress(Math.min(1, st.chord / 1)); if (st.chord >= 1) { st.chord = 0; const c = capture; releaseCapture(); c.onExit && c.onExit(); } }
    else if (st.chord) { st.chord = 0; capture.onProgress && capture.onProgress(0); }
    return;
  }
  const active = edges.length || dir || Math.hypot(rx, ry) > .35;
  if (!mode) { if (active) { setMode(true); st.dir = dir; st.next = ts + DELAY; st.since = ts; } return; }
  const hk = hooked();
  for (const i of edges) if (i < 12 || i > 15) { if (listeners.size && consumed) { consumed = false; continue; } if (hk && hk.press(NAMES[i])) { SH.sound.gesture(); continue; } press(i); }
  if (dir) {
    if (dir !== st.dir) { st.dir = dir; st.next = ts + DELAY; st.since = ts; move(dir); }
    else if (ts >= st.next) { st.next = ts + (ts - st.since > 1400 ? FAST : REPEAT); move(dir); }
  } else st.dir = null;
  if (hk) hk.stick(rx, ry, dt, n => !!bs[B[n]]);
  else {
    /* the view that had the stick lost it while the stick was still deflected (the focus moved on, a panel was entered):
       tell it once that the stick is at rest, so it settles (the 3D model glides back) instead of staying where it was */
    if (lastHk && lastHk !== hook) lastHk = null;
    if (lastHk) { try { lastHk.stick(0, 0, dt, () => false); } catch (e) { console.error(e); } }
    if (Math.hypot(rx, ry) > RDZ) stickScroll(rx, ry, dt);
  }
  lastHk = hk;
}
let lastHk = null;
let consumed = false, scrollerCache = null;
/* ---------- a view that takes the right stick and a few buttons while the focus is in it (the Home 3D model orbits) ----------
   SH.gamepad.setHook({ owns(el) -> true while it wants them (el = the focus), stick(x, y, dt, held) every frame (raw axes,
   held(name) -> that button is down), press(name) -> true when it used the press, hints() -> [[glyphs, label], ...] }) */
let hook = null, hkAt = 0, hkVal = null;
function hooked() {
  const now = performance.now(); if (now - hkAt < 150) return hkVal; hkAt = now;
  return (hkVal = hook && !capture && !editing() && !insideRoot() && !state.palOpen && !state.setOpen && scope() === document.body && hook.owns(cur) ? hook : null);
}
function stickScroll(rx, ry, dt) {
  if (editing()) return;
  const k = v => { const a = Math.abs(v); return Math.sign(v) * ((a - RDZ) / (1 - RDZ)) ** 2; };
  /* whatever the user is in: the panel entered, else the page, else the panel the focus is on, else what is under
     the middle of the window. The focus stays where it is (the next D-pad move carries on from what is on screen). */
  if (!scrollerCache || !scrollerCache.isConnected) {
    const p = cur && cur.isConnected && isPanel(cur) ? cur : null;
    scrollerCache = insideRoot() || pageScroller() || p || (cur && usable(cur) && scrollerOf(cur)) || scrollerOf(document.elementFromPoint(innerWidth / 2, innerHeight / 2) || document.body) || false;
  }
  const s = scrollerCache; if (!s) return;
  s.scrollBy({ top: k(ry) * 1100 * dt, left: k(rx) * 1100 * dt, behavior: 'instant' }); frameN = 0;
}
function tickRing(dt) {
  if (!cur) { ring.classList.remove('has'); return; }
  ring.classList.add('has');
  frameN++;
  /* the target is re-measured every frame for a moment after a move or a scroll, then every few frames */
  if (frameN < 40 || frameN % 8 === 0) {
    if (!cur.isConnected) return;
    const r = rectOf(cur);
    /* scrolled out of sight (right stick, wheel): the ring waits off, the next move starts from what is on screen */
    if (frameN % 6 === 1) {   /* the heavier visibility check: a few times a second, not every frame */
      const seen = cur === insideRoot() || inView(cur); ring.classList.toggle('gone', !seen); tip.classList.toggle('gone', !seen);
      placeFrame(); placeTip();
    }
    if (!ring.classList.contains('gone')) lastRect = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    rs.x.t = r.left; rs.y.t = r.top; rs.w.t = r.width; rs.h.t = r.height;
  }
  if (ringSnap) { ringSnap = false; rs.x.set(rs.x.t); rs.y.set(rs.y.t); rs.w.set(rs.w.t); rs.h.set(rs.h.t); }
  else { rs.x.step(dt); rs.y.step(dt); rs.w.step(dt); rs.h.step(dt); }
  const st = `translate3d(${rs.x.v.toFixed(1)}px,${rs.y.v.toFixed(1)}px,0)|${Math.max(4, rs.w.v).toFixed(1)}|${Math.max(4, rs.h.v).toFixed(1)}`;
  if (st !== lastRingStyle) { lastRingStyle = st; const [tr, w, h] = st.split('|'); ring.style.transform = tr; ring.style.width = w + 'px'; ring.style.height = h + 'px'; }
}

/* ---------- leaving controller mode: any real mouse, touch or keyboard use ---------- */
const off = () => { if (mode) setMode(false); };
addEventListener('pointermove', e => { if (!e.isTrusted) return; const moved = Math.abs(e.clientX - lx) + Math.abs(e.clientY - ly) > 3; lx = e.clientX; ly = e.clientY; if (moved) off(); }, { capture: true, passive: true });
addEventListener('pointerdown', e => { if (e.isTrusted) off(); }, { capture: true, passive: true });
addEventListener('wheel', e => { if (e.isTrusted) off(); }, { capture: true, passive: true });
addEventListener('keydown', e => { if (e.isTrusted) off(); }, { capture: true });

/* ---------- keyboard: Page Up / Down and Home / End scroll the innermost area around the focus that can still move
   that way (a list, then the page), not only the one the browser picks ---------- */
addEventListener('keydown', e => {
  if (!/^(PageUp|PageDown|Home|End)$/.test(e.key) || e.ctrlKey || e.altKey || e.metaKey || e.defaultPrevented) return;
  const a = document.activeElement;
  if (isTextField(a) || (a && (a.tagName === 'SELECT' || (a.tagName === 'INPUT' && a.type === 'range') || a.closest('.csel-pop,.pal')))) return;
  const d = e.key === 'PageDown' || e.key === 'End' ? 1 : -1, all = e.key === 'Home' || e.key === 'End';
  const start = a && a !== document.body ? a : document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  for (let p = start; p && p !== document.documentElement; p = p.parentElement) {
    if (p.scrollHeight - p.clientHeight <= 2 || !/(auto|scroll)/.test(getComputedStyle(p).overflowY)) continue;
    const max = p.scrollHeight - p.clientHeight; if ((d > 0 && p.scrollTop >= max - 1) || (d < 0 && p.scrollTop <= 1)) continue;
    e.preventDefault();
    p.scrollTo({ top: all ? (d > 0 ? max : 0) : p.scrollTop + d * p.clientHeight * .85, behavior: isMin() ? 'instant' : 'smooth' });
    return;
  }
});

/* ---------- connection ---------- */
function scan() {
  const l = navigator.getGamepads ? navigator.getGamepads() : [];
  for (const gp of l) if (gp && gp.connected && !pads.has(gp.index)) { pads.set(gp.index, { btn: [], primed: false, dir: null, next: 0, since: 0, chord: 0 }); detectLayout(gp); }
}
addEventListener('gamepadconnected', e => {
  SH.sound('device_connected');
  if (!enabled()) return;
  pads.set(e.gamepad.index, { btn: [], primed: false, dir: null, next: 0, since: 0, chord: 0 }); detectLayout(e.gamepad);
  if (!sessionToast && !S().gamepadSeen) { sessionToast = true; SH.setSetting('gamepadSeen', true); SH.toast(t('Controller connected: you can use RsHub with it'), { group: 'pad', ms: 5200 }); }
  kick();
});
addEventListener('gamepaddisconnected', e => {
  SH.sound('device_disconnected');
  pads.delete(e.gamepad.index); scan();
  if (!pads.size) { if (mode) SH.toast(t('Controller disconnected'), { group: 'pad', ms: 2600 }); setMode(false); }
  kick();
});
addEventListener('focus', () => { scan(); kick(); }); addEventListener('blur', kick);
document.addEventListener('visibilitychange', () => { scan(); kick(); });
SH.on('setting', ({ key }) => { if (key === 'gamepadNav') { if (enabled()) scan(); else { pads.clear(); capture = null; } kick(); } });
SH.on('boot', () => { scan(); kick(); });
/* the ring follows the interface when it changes language, zoom, size */
addEventListener('resize', () => { frameN = 0; });

function releaseCapture() { capture = null; if (mode) { ring.classList.add('on'); showHints(true); hintKey = ''; paintHints(); aimRing(true); paintPanel(); paintTip(); } }

/* ---------- modal dialogs: focus trap, inert background, focus back to the trigger ----------
   Generic: whichever dialog is open on top (palette, settings, component sheet, update, confirmations, dialogs inside
   a module) gets the rest of the app made inert, Tab stays inside it, and when it closes the focus goes back to
   what opened it. Works for keyboard users too, with or without a controller. */
const MODALS = ['.su-ov', '.cs-o.on', '.hg-dlg', '.mdlg', '.bk-dq-o', '.dq-o', '.u-confirm', '.bug-pop', '.ar-o'];
const KEEP = '#toasts,#padRing,#padHints,#padPanel,#padTip,script,style,link';
let modal = null, opener = null, inerted = [], lastFocus = null, modalTimer = 0;
const under = []; let inModal = null;  /* [dialog, its opener] for each dialog still open under the one on top (Settings under the update dialog) */
function modalTop() {
  if (state.palOpen) return $('#palO');
  let b = null, bz = -1;
  for (const sel of MODALS) for (const o of $$(sel)) {
    if (!visible(o)) continue;
    const z = (parseInt(getComputedStyle(o).zIndex, 10) || 0) + (o.closest('.view') || o.closest('#sheet') ? 0 : 100);
    if (z >= bz) { b = o; bz = z; }
  }
  if (b) return b;
  return state.setOpen ? $('#setO') : null;
}
const tabbables = root => $$('a[href],button,input:not([type=hidden]),select,textarea,[tabindex]:not([tabindex="-1"])', root).filter(el => !el.disabled && visible(el) && !el.closest('[inert]'));
function releaseModal(stacked) {
  inerted.forEach(el => el.removeAttribute('inert')); inerted = [];
  const o = opener, was = modal; modal = null; opener = null; clearInterval(modalTimer); modalTimer = 0;
  if (stacked) return;  /* a dialog opened on top of it: the focus moves into that one, never to the page behind both */
  const a = document.activeElement;
  if (o && o.isConnected && visible(o) && (!a || a === document.body || (was && was.contains(a)))) { try { o.focus({ preventScroll: true }); } catch {} }
}
function syncModal() {
  const top = modalTop();
  if (top === modal) return;
  /* a new dialog over one that stays open: what had the focus in the lower one opened it, and gets it back when it closes */
  /* (the last focus inside the lower one: the new dialog may already have taken the focus to its own button) */
  const lower = modal && top && visible(modal) ? modal : null, from = inModal && inModal.isConnected ? inModal : document.activeElement;
  inModal = null;
  if (lower) under.push([lower, opener]);
  if (modal) releaseModal(!!lower);
  if (!top) { under.length = 0; return; }
  const i = under.findIndex(([d]) => d === top);
  modal = top;
  if (i >= 0) { opener = under[i][1]; under.length = i; }
  else opener = lower && from && lower.contains(from) ? from : lastFocus && lastFocus.isConnected && !top.contains(lastFocus) ? lastFocus : null;
  for (let n = top; n && n !== document.body; n = n.parentElement) for (const sib of n.parentElement ? n.parentElement.children : []) {
    if (sib !== n && !sib.matches(KEEP) && !sib.hasAttribute('inert')) { sib.setAttribute('inert', ''); inerted.push(sib); }
  }
  const a = document.activeElement;
  if (!a || a === document.body || !top.contains(a)) {
    const l = tabbables(top), f = l.find(el => el.classList.contains('pri')) || l[0];
    if (f && !isTextField(f)) { try { f.focus({ preventScroll: true }); } catch {} }
  }
  if (!modalTimer) modalTimer = setInterval(() => { if (!document.hidden) syncModal(); }, 160);  /* (in the tray: nothing to check) */
}
const syncSoon = () => { if (syncSoon.t) return; syncSoon.t = setTimeout(() => { syncSoon.t = 0; syncModal(); }, 60); };
document.addEventListener('focusin', e => { if (modal && modal.contains(e.target)) inModal = e.target; if (!modal && e.target !== document.body && !(e.target.closest && e.target.closest(MODALS.join(',') + ',#palO,#setO'))) lastFocus = e.target; }, true);
addEventListener('keydown', e => {
  if (e.key !== 'Tab' || !modal) return;
  const list = tabbables(modal); if (!list.length) { e.preventDefault(); return; }
  const a = document.activeElement, i = list.indexOf(a);
  if (i < 0) { e.preventDefault(); list[e.shiftKey ? list.length - 1 : 0].focus(); }
  else if (e.shiftKey && i === 0) { e.preventDefault(); list[list.length - 1].focus(); }
  else if (!e.shiftKey && i === list.length - 1) { e.preventDefault(); list[0].focus(); }
}, true);
new MutationObserver(syncSoon).observe(document.body, { childList: true, subtree: true });
['#palO', '#setO'].forEach(q => { const o = $(q); if (o) new MutationObserver(syncSoon).observe(o, { attributes: true, attributeFilter: ['class'] }); });
SH.modal = { sync: syncModal, get open() { return modal; } };

/* ---------- the API for modules ---------- */
SH.gamepad = {
  enabled,
  get active() { return mode; },
  get connected() { return [...(navigator.getGamepads ? navigator.getGamepads() : [])].filter(g => g && g.connected); },
  /* take the buttons away from the navigation (a test that needs to see every press): capture({ onExit, onProgress }).
     The user gets them back by holding View + Menu for a second (onExit runs), or you call release(). */
  capture(o = {}) { capture = o; ring.classList.remove('on'); showHints(false); paintPanel(); paintTip(); kick(); },
  release() { if (capture) releaseCapture(); },
  get captured() { return !!capture; },
  /* every button press while a controller is connected: fn({ index, name, pad }) */
  onButton(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  focus(el) { if (el) { cur = null; setCur(el, true); } },
  /* a view that takes the right stick and some buttons while the focus is in it: see hooked() above; null removes it */
  setHook(h) { hook = h || null; hkAt = 0; hintKey = ''; if (mode) paintHints(); },
  get hook() { return hook; },
  move, glyph, BUTTONS: B,
  /* extra selectors that count as an overlay (a custom dialog), so the focus stays inside it */
  addOverlay(sel) { custom.push(sel); },
  /* the panel the focus is inside (A entered it), or null */
  get inside() { return insideRoot(); },
  /* for tests and audits: the targets the D-pad can reach from here and where a move would go */
  debug: { candidates: () => candidates(scope(), insideRoot()), candidatesIn: el => candidates(scope(), el), best: (el, dir, list) => best(el, list || candidates(scope(), insideRoot()), dir), isPanel: el => isPanel(el), scope, get cur() { return cur; }, inView },
};
})();

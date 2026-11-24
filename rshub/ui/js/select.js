/* RsHub · custom dropdown for every <select> in the app.
   Progressive: the real <select> stays in the page (hidden), so module code keeps reading .value and listening to
   'change'. A button takes its place and opens an animated list in the app's style, with keyboard support
   (arrows, Home/End, Enter, Esc, type to jump). New selects are picked up automatically as modules render. */
'use strict';
(() => {
const { anim, EASE } = SH;
const CHEV = '<svg class="i csel-chev" viewBox="0 0 24 24"><path d="m7 10 5 5 5-5"/></svg>';
const CHECK = '<svg class="i" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>';
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const VALUE = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
const INDEX = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
let openState = null; // { sel, btn, pop, items, active }

function label(sel) { const o = sel.options[INDEX.get.call(sel)]; return o ? o.textContent : ''; }

function enhance(sel) {
  if (sel.dataset.csel || sel.multiple || sel.size > 1) return;
  sel.dataset.csel = '1';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = (sel.className ? sel.className + ' ' : '') + 'csel';
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  if (sel.getAttribute('aria-label')) btn.setAttribute('aria-label', sel.getAttribute('aria-label'));
  sel.after(btn);
  sel.classList.add('csel-native');
  sel.tabIndex = -1;
  const refresh = () => {
    btn.innerHTML = `<span class="csel-v">${esc(label(sel)) || '&nbsp;'}</span>${CHEV}`;
    btn.disabled = sel.disabled;
    btn.title = label(sel);
  };
  sel._cselRefresh = refresh;
  /* code that sets .value or .selectedIndex directly does not fire events: keep the button in sync anyway */
  Object.defineProperty(sel, 'value', { configurable: true, get() { return VALUE.get.call(this); }, set(v) { VALUE.set.call(this, v); refresh(); } });
  Object.defineProperty(sel, 'selectedIndex', { configurable: true, get() { return INDEX.get.call(this); }, set(v) { INDEX.set.call(this, v); refresh(); } });
  new MutationObserver(refresh).observe(sel, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled'] });
  sel.addEventListener('change', refresh);
  btn.addEventListener('click', e => { e.stopPropagation(); openState && openState.sel === sel ? close() : open(sel, btn); });
  btn.addEventListener('keydown', e => {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); open(sel, btn); }
  });
  refresh();
}

function open(sel, btn) {
  close(true);
  if (sel.disabled || !sel.options.length) return;
  const pop = document.createElement('div');
  pop.className = 'csel-pop';
  pop.setAttribute('role', 'listbox');
  const cur = INDEX.get.call(sel);
  pop.innerHTML = [...sel.options].map((o, i) =>
    `<button type="button" role="option" class="csel-o${i === cur ? ' on' : ''}" data-i="${i}"${o.disabled ? ' disabled' : ''} aria-selected="${i === cur}">` +
    `<span>${esc(o.textContent)}</span>${CHECK}</button>`).join('');
  document.body.appendChild(pop);
  const r = btn.getBoundingClientRect(), vh = innerHeight, vw = innerWidth;
  pop.style.minWidth = Math.max(160, r.width) + 'px';
  const h = Math.min(pop.scrollHeight, 320), below = vh - r.bottom - 12, up = below < h && r.top > below;
  pop.style.maxHeight = Math.max(120, Math.min(320, up ? r.top - 12 : below)) + 'px';
  pop.style.left = Math.min(r.left, vw - pop.offsetWidth - 8) + 'px';
  pop.style.top = (up ? r.top - Math.min(h, r.top - 12) - 6 : r.bottom + 6) + 'px';
  pop.style.transformOrigin = up ? 'bottom left' : 'top left';
  const items = [...pop.querySelectorAll('.csel-o')];
  openState = { sel, btn, pop, items, active: Math.max(0, cur) };
  btn.setAttribute('aria-expanded', 'true');
  btn.classList.add('open');
  items[openState.active] && items[openState.active].scrollIntoView({ block: 'nearest' });
  setActive(openState.active);
  SH.sound('dropdown');
  anim(pop, [{ opacity: 0, transform: `translate3d(0,${up ? 6 : -6}px,0) scale(.96)` }, { opacity: 1, transform: 'none' }], { d: 260, e: EASE.spring });
  pop.addEventListener('click', e => { const b = e.target.closest('.csel-o'); if (b && !b.disabled) choose(+b.dataset.i); });
  pop.addEventListener('mousemove', e => { const b = e.target.closest('.csel-o'); if (b) setActive(+b.dataset.i, false); });
}

function setActive(i, scroll = true) {
  if (!openState) return;
  const { items } = openState;
  i = Math.max(0, Math.min(items.length - 1, i));
  items.forEach((b, j) => b.classList.toggle('act', j === i));
  openState.active = i;
  if (scroll && items[i]) items[i].scrollIntoView({ block: 'nearest' });
}

function choose(i) {
  if (!openState) return;
  const { sel, btn } = openState;
  const changed = INDEX.get.call(sel) !== i;
  INDEX.set.call(sel, i);
  sel._cselRefresh();
  close();
  btn.focus({ preventScroll: true });
  if (changed) { sel.dispatchEvent(new Event('input', { bubbles: true })); sel.dispatchEvent(new Event('change', { bubbles: true })); }
}

function close(instant) {
  if (!openState) return;
  const { pop, btn } = openState;
  openState = null;
  btn.setAttribute('aria-expanded', 'false');
  btn.classList.remove('open');
  if (instant) { pop.remove(); return; }
  anim(pop, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.97)' }], { d: 140 }).finished.then(() => pop.remove(), () => pop.remove());
}

let typed = '', typedAt = 0;
addEventListener('keydown', e => {
  if (!openState) return;
  const { items, active } = openState;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); const b = openState.btn; close(); b.focus({ preventScroll: true }); return; }
  if (e.key === 'Tab') { close(); return; }
  if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); return; }
  if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); return; }
  if (e.key === 'Home') { e.preventDefault(); setActive(0); return; }
  if (e.key === 'End') { e.preventDefault(); setActive(items.length - 1); return; }
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (!items[active].disabled) choose(active); return; }
  if (e.key.length === 1) { /* type to jump */
    const now = Date.now(); typed = (now - typedAt > 700 ? '' : typed) + e.key.toLowerCase(); typedAt = now;
    const hit = items.findIndex(b => b.textContent.trim().toLowerCase().startsWith(typed));
    if (hit >= 0) setActive(hit);
  }
}, true);
addEventListener('mousedown', e => { if (openState && !openState.pop.contains(e.target) && !openState.btn.contains(e.target)) close(); }, true);
addEventListener('wheel', e => { if (openState && !openState.pop.contains(e.target)) close(); }, { passive: true });
addEventListener('resize', () => close(true));
addEventListener('blur', () => close(true));

function scan(root) {
  if (root.tagName === 'SELECT') enhance(root);
  else if (root.querySelectorAll) root.querySelectorAll('select').forEach(enhance);
}
new MutationObserver(list => {
  for (const m of list) for (const n of m.addedNodes) if (n.nodeType === 1) scan(n);
}).observe(document.documentElement, { childList: true, subtree: true });
scan(document.documentElement);
SH.enhanceSelects = scan;
})();

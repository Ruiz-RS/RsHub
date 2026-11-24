/* RsHub · administrator prompt and resume.

   SH.requireAdmin(reason, { resume }) — before anything that needs administrator rights. In administrator mode it
   answers true. Otherwise it answers false and opens a dialog in the middle of the window: "This needs administrator
   mode", the reason ("to install drivers."), Not now (B, Esc) and Open as administrator (A, Enter). The latter reopens
   RsHub elevated (the start screen hands over, Windows asks first) and the new copy comes back to the same place:
   module, tab, settings section, Home view, the text in the search fields and the scroll positions. If the call named
   an action, the new copy runs it again: resume = { action: 'drivers.install', args: { ids: [...] } }.

   SH.adminFirst(reason, { resume }) — the same dialog for work that can go on without administrator rights (the
   steps that need them are skipped): resolves true to go on (administrator mode, or Not now), false when RsHub
   is reopening as administrator.

   SH.resume.register('drivers.install', async (args, r) => { ... }) — the actions that can be run again. The ticket
   that carries them to the elevated copy is a file in the user's data folder (rshub/resume.py: bound to the new
   copy's command line, valid for two minutes, read once, only action ids from the list in resume.py), but it is still
   data from outside: an action never trusts args. It rebuilds the click from the module's own state now (the drivers
   it lists, the device it shows), checks every id in args against it, and goes through the module's normal path, so
   the same confirmation ("Install 2 drivers?") is asked again. If what it acted on is gone or changed it does nothing
   and says so (r.changed()). Return false for "changed"; anything else counts as done.
   r: until(fn, ms) waits for fn() to be truthy (null when it never is), open(mod, tab) shows a tab and waits for its
   panel, str(x) / strs(x) / num(x) read args safely, changed() says it was not run.

   Sample mode (a browser, no Python): Open as administrator reloads the page as a pretend administrator copy that
   resumes the same way (sessionStorage); ?uac=deny pretends Windows said no; ?normal=1 makes the modules that only ask
   inside the app (drivers, backups) ask in sample mode too. */
'use strict';
(() => {
const { $, $$, state, anim, EASE, esc, ic } = SH;
const t = SH.t;
const IN_APP = !!(window.chrome && window.chrome.webview);
const ACTION = /^[a-z]{2,24}\.[a-z][A-Za-z]{1,40}$/;
const MAX_AGE = 120000;
const actions = new Map();
const wait = ms => new Promise(r => setTimeout(r, ms));
const query = new URLSearchParams(location.search);
SH.sampleNormal = !IN_APP && query.get('normal') === '1';

function register(id, run) {
  if (!ACTION.test(id) || typeof run !== 'function') throw new Error('bad resume action: ' + id);
  actions.set(id, run);
}

/* ---------- where the user is ---------- */
const ID_SEL = /^#[A-Za-z][\w-]{0,60}$/;
const activeTab = () => { const v = $('#v-' + state.view), b = v && $('.subnav [data-tab].on', v); return b ? b.dataset.tab : ''; };
const settingsSec = () => { const b = state.setOpen && $('#sheet nav [data-sec].on'); return b ? b.dataset.sec : ''; };
/* the areas whose scroll is kept: the view (compact layout), the open tab, the settings body, and anything with an id
   inside them that is scrolled */
function scrollRoots() {
  const out = [], v = $('#v-' + state.view), tab = v && $('.tab.on', v), body = state.setOpen ? $('#setBody') : null;
  if (v) out.push(['view', v]); if (tab) out.push(['tab', tab]); if (body) out.push(['settings', body]);
  return out;
}
function ticket(o = {}) {
  const tk = { v: 1, at: Date.now(), module: o.module || state.view || 'home', tab: o.tab != null ? o.tab : (o.module && o.module !== state.view ? '' : activeTab()),
    settings: o.settings != null ? o.settings : settingsSec(), home: String(state.settings.homeVariant || ''), scroll: [], fields: [], action: '', args: {} };
  if (!/^[a-z0-9]{1,24}$/.test(tk.home)) tk.home = '';
  for (const [k, el] of scrollRoots()) {
    if (el.scrollTop > 0) tk.scroll.push({ k, y: Math.round(el.scrollTop) });
    for (const x of $$('[id]', el)) {
      if (tk.scroll.length >= 16) break;
      if (x.scrollTop > 0 && ID_SEL.test('#' + x.id) && !tk.scroll.some(s => s.k === '#' + x.id)) tk.scroll.push({ k: '#' + x.id, y: Math.round(x.scrollTop) });
    }
  }
  for (const [, el] of scrollRoots().filter(([k]) => k !== 'view')) {
    for (const f of $$('input[id]', el)) {
      const ty = (f.getAttribute('type') || 'text').toLowerCase();
      if (tk.fields.length >= 8 || !['text', 'search'].includes(ty) || !f.value || f.value.length > 200 || !ID_SEL.test('#' + f.id)) continue;
      if (!tk.fields.some(x => x.k === '#' + f.id)) tk.fields.push({ k: '#' + f.id, v: f.value });
    }
  }
  if (o.action && ACTION.test(o.action) && actions.has(o.action)) { tk.action = o.action; tk.args = o.args && typeof o.args === 'object' ? o.args : {}; }
  return tk;
}

/* ---------- the dialog ---------- */
let dlg = null;
SH.icons.add({ adminup: '<path d="M12 3 4.5 6v5.5c0 4.5 3.2 8 7.5 9.5 4.3-1.5 7.5-5 7.5-9.5V6z"/><path d="M12 15.5v-7m-3 3 3-3 3 3"/>' });
function close(v) {
  const d = dlg; if (!d) return; dlg = null;
  removeEventListener('keydown', d.key, true);
  anim($('.ar-s', d.o), [{ opacity: 1 }, { opacity: 0 }], { d: 180 });
  anim($('.ar', d.o), [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translate3d(0,8px,0) scale(.97)' }], { d: 180 })
    .finished.then(() => d.o.remove()).catch(() => d.o.remove());
  d.res(v);
}
/* resolves 'later' (Not now, B, Esc, a click outside), 'relaunch' (RsHub is reopening) or 'stay' (Windows said no, or
   something still runs: this copy goes on as it was) */
function prompt(reason, o = {}) {
  if (dlg) return dlg.p;
  const tk = ticket(o.resume || {});
  const o2 = document.createElement('div'); o2.className = 'ar-o';
  const rerun = !!tk.action;
  o2.innerHTML = `<div class="ar-s"></div><div class="ar pan" role="dialog" aria-modal="true" aria-labelledby="arT" aria-describedby="arD">
    <span class="ar-ic">${ic('adminup')}</span>
    <h3 id="arT">${esc(t('This needs administrator mode'))}</h3>
    <p id="arD"><b>${esc(t('Open RsHub as administrator'))}</b> ${esc(reason || t('to make this change in Windows.'))}</p>
    <p>${esc(rerun ? t('Windows asks for permission, then RsHub reopens right here and picks up what you asked for.') : t('Windows asks for permission, then RsHub reopens right here.'))}</p>
    <button class="ar-always" role="checkbox" aria-checked="false" data-ar-always><span class="check"><svg class="i" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg></span><span>${esc(t('Always open as administrator'))}</span></button>
    <p class="ar-note" data-ar-note hidden>${esc(t('Windows asks each time.'))}</p>
    <div class="ar-a"><button class="btn ghost" data-close>${esc(o.later || t('Not now'))}</button><button class="btn pri" data-ar-go>${ic('shield')}${esc(t('Open as administrator'))}</button></div></div>`;
  document.body.appendChild(o2);
  anim($('.ar-s', o2), [{ opacity: 0 }, { opacity: 1 }], { d: 220 });
  anim($('.ar', o2), [{ opacity: 0, transform: 'translate3d(0,14px,0) scale(.96)', filter: 'blur(6px)' }, { opacity: 1, transform: 'none', filter: 'blur(0px)' }], { d: 420, e: EASE.spring });
  SH.sound('unavailable');
  let res; const p = new Promise(r => { res = r; });
  const key = e => {
    if (!dlg || dlg.o !== o2) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); if (!o2.dataset.busy) close('later'); }
  };
  dlg = { o: o2, p, res, key };
  addEventListener('keydown', key, true);
  o2.addEventListener('click', async e => {
    if (o2.dataset.busy) return;
    if (e.target.closest('[data-close]') || e.target.classList.contains('ar-s')) { close('later'); return; }
    /* unticked by default: this session only. Ticked, it turns on Settings > Administrator > Always open as
       administrator; a per-user install (no task) then says Windows asks each time */
    const al = e.target.closest('[data-ar-always]');
    if (al) { const on = al.getAttribute('aria-checked') !== 'true'; al.setAttribute('aria-checked', String(on)); $('.check', al).classList.toggle('on', on);
      $('[data-ar-note]', o2).hidden = !(on && state.adminTask === false); return; }
    const go = e.target.closest('[data-ar-go]'); if (!go) return;
    const always = $('[data-ar-always]', o2).getAttribute('aria-checked') === 'true';
    o2.dataset.busy = '1'; go.disabled = true; go.innerHTML = `${ic('shield')}${esc(t('Opening…'))}`;
    const r = await relaunch(ticket(o.resume || {}), always);
    delete o2.dataset.busy;
    close(r === true ? 'relaunch' : 'stay');
  });
  setTimeout(() => { const b = $('[data-ar-go]', o2); if (b && dlg && dlg.o === o2) b.focus({ preventScroll: true }); if (SH.modal) SH.modal.sync(); }, 40);
  return p;
}

/* ---------- the relaunch ---------- */
/* true: Windows said yes and this copy closes; false: it said no; 'busy': a task still runs here */
async function relaunch(tk, always = false) {
  /* something the user started still runs here (a queue, a scan): reopening now would cut it off */
  if (SH.activity.list().some(a => a.user && !a.bg)) { SH.toast(t('Something you started is still running. Open as administrator when it finishes.'), { kind: 'warning' }); return 'busy'; }
  if (!IN_APP && !(window.pywebview && window.pywebview.api)) return simulate(tk, always);
  const r = SH.relaunchAdmin ? await SH.relaunchAdmin(tk, { always }) : await SH.call('relaunch_admin', tk);
  if (r === true) { SH.toast(t('Opening RsHub as administrator…')); return true; }
  if (r === 'busy') { SH.toast(t('Something you started is still running. Open as administrator when it finishes.'), { kind: 'warning' }); return 'busy'; }
  SH.toast(t("Windows didn't give permission. You're still in normal mode."), { kind: 'warning' });
  return false;
}
/* sample mode: the page reloads as a pretend administrator copy that resumes from sessionStorage */
async function simulate(tk, always) {
  if (query.get('uac') === 'deny') { await wait(300); SH.toast(t("Windows didn't give permission. You're still in normal mode."), { kind: 'warning' }); return false; }
  try { sessionStorage.setItem('sh.resume', JSON.stringify(tk)); sessionStorage.setItem('sh.sampleAdmin', '1'); } catch { return false; }
  if (always) SH.setSetting('alwaysAdmin', true);
  SH.toast(t('Opening RsHub as administrator…'));
  await wait(500);
  const a = SH.leave ? SH.leave(false) : null;
  try { await a; } catch {}
  location.reload();
  return true;
}

/* ---------- the elevated copy: back to the same place ---------- */
function valid(tk) {
  if (!tk || typeof tk !== 'object' || tk.v !== 1) return null;
  let at = +tk.at; if (at > 0 && at < 1e11) at *= 1000;  /* Python stamps it in seconds when it saves it */
  if (!(at > 0) || Math.abs(Date.now() - at) > MAX_AGE) return null;
  const s = (x, rx) => typeof x === 'string' && rx.test(x) ? x : '';
  const out = { v: 1, at, module: s(tk.module, /^[a-z]{2,24}$/), tab: s(tk.tab, /^[a-z]{1,24}$/), settings: s(tk.settings, /^[a-z]{2,24}$/), home: s(tk.home, /^[a-z0-9]{1,24}$/),
    scroll: (Array.isArray(tk.scroll) ? tk.scroll : []).slice(0, 16).filter(x => x && (/^(view|tab|settings)$/.test(x.k) || ID_SEL.test(x.k)) && Number.isFinite(x.y) && x.y >= 0),
    fields: (Array.isArray(tk.fields) ? tk.fields : []).slice(0, 8).filter(x => x && ID_SEL.test(x.k) && typeof x.v === 'string' && x.v.length <= 200),
    action: typeof tk.action === 'string' && ACTION.test(tk.action) && actions.has(tk.action) ? tk.action : '',
    args: tk.args && typeof tk.args === 'object' && !Array.isArray(tk.args) ? tk.args : {} };
  return out;
}
/* once, at boot: the ticket the normal copy left (Python checks it first: rshub/resume.py), or null */
async function take() {
  let tk = null;
  if (IN_APP || (window.pywebview && window.pywebview.api)) tk = await SH.call('resume_take');
  else { try { tk = JSON.parse(sessionStorage.getItem('sh.resume') || 'null'); sessionStorage.removeItem('sh.resume'); } catch { tk = null; } }
  return valid(tk);
}
const until = async (fn, ms = 10000, step = 120) => { const end = Date.now() + ms; for (;;) { let v = null; try { v = fn(); } catch { v = null; } if (v) return v; if (Date.now() > end) return null; await wait(step); } };
function findScroll(tk, k) {
  if (k === 'view') return $('#v-' + tk.module);
  if (k === 'tab') { const v = $('#v-' + tk.module); return v && $('.tab.on', v); }
  if (k === 'settings') return $('#setBody');
  return document.getElementById(k.slice(1));
}
function changed() { SH.toast(t('That changed while RsHub reopened, so it was not done. Check it and try again.'), { kind: 'warning', ms: 7000 }); }
const helpers = {
  until, wait, changed,
  str: (x, max = 400) => typeof x === 'string' && x.length <= max ? x : '',
  strs: (x, max = 300) => Array.isArray(x) ? x.filter(v => typeof v === 'string' && v.length <= 400).slice(0, max) : [],
  num: (x, lo = -Infinity, hi = Infinity) => typeof x === 'number' && isFinite(x) ? Math.min(hi, Math.max(lo, x)) : null,
  /* shows module/tab and answers its panel once the module has drawn it (null if it never does) */
  async open(mod, tab) {
    if (state.view !== mod) SH.go(mod, { tab, silent: true }); else if (tab) SH.go(mod, { tab });
    return until(() => { const p = $(`#v-${mod} .tab[data-tab="${tab}"]`); return p && p.classList.contains('on') && p.children.length ? p : null; }, 8000);
  },
};
let resuming = false;
async function restore(tk) {
  if (!tk) return;
  resuming = true;
  try {
    if (tk.home && SH.registry.variants[tk.home] && state.settings.homeVariant !== tk.home) SH.setSetting('homeVariant', tk.home);
    if (tk.module && tk.module !== 'home' && SH.modById(tk.module)) SH.go(tk.module, tk.tab ? { tab: tk.tab, silent: true } : { silent: true });
    if (tk.settings) { await wait(200); SH.go('settings', { section: tk.settings }); }
    /* the search fields: their text, and the module filters as if it had been typed */
    for (const f of tk.fields) {
      const el = await until(() => { const x = document.getElementById(f.k.slice(1)); return x && x.isConnected ? x : null; }, 5000);
      if (el && el.tagName === 'INPUT' && !el.value) { el.value = f.v; el.dispatchEvent(new Event('input', { bubbles: true })); }
    }
    /* the scroll positions: the lists fill in as their data arrives, so each one is tried until it can go that far */
    const left = new Map(tk.scroll.map(s => [s.k, s.y]));
    await until(() => {
      for (const [k, y] of left) {
        const el = findScroll(tk, k); if (!el) continue;
        if (el.scrollHeight - el.clientHeight >= y - 2) { el.scrollTop = y; left.delete(k); }
      }
      return !left.size;
    }, 4000, 150);
    if (tk.action) {
      const run = actions.get(tk.action);
      let ok = false;
      try { ok = await run(tk.args || {}, helpers); } catch (e) { console.error(e); ok = false; }
      if (ok === false) changed();
    } else SH.toast(t("You're in administrator mode now. Pick up where you left off."), { kind: 'success' });
  } finally { resuming = false; }
}

/* ---------- the public calls ---------- */
SH.requireAdmin = (reason, o = {}) => { if (state.admin) return true; prompt(reason, o); return false; };
SH.adminFirst = async (reason, o = {}) => { if (state.admin) return true; const r = await prompt(reason, o); return r !== 'relaunch'; };
SH.resume = { register, ticket, prompt, take, restore, valid, simulate, actions: () => [...actions.keys()], get open() { return dlg ? dlg.o : null; }, get resuming() { return resuming; }, helpers };
})();

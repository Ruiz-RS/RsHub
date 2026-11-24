"""Actions on the right item and answers honoured (round 7 of the UI review): the pieces are taken from the real files
and run in Node against a small stand-in for the DOM.

- A toast refreshed by a repeated message runs the LATEST action: two results deleted (or two devices disabled) in a
  row, its Undo brings back the second one, not the first.
- Driver Updater's confirmation: Enter on Cancel cancels; a held Enter (auto-repeat) never confirms.
- Program Updater's "Update all" confirmation: Esc closes it (instead of going Home behind it), a double click on
  Update queues once, and the dialog is a known overlay for the controller, the focus trap and the dialog sounds.
- Storage Checker's duplicates: a retry after a partial clean skips the copies already in the Recycle Bin and finishes
  the group; a copy already in the Recycle Bin cannot become the one kept.
- Program Remover's leftover review answers once (Keep all while the leftovers are being moved does nothing).
- Live Monitor: a DNS change the engine applied only in part is an error whose toast still offers Undo."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "rshub", "ui")


def read(*p):
    with open(os.path.join(UI, *p), encoding="utf-8") as fh:
        return fh.read()


def js_function(src, name):
    """The source of `function name(...) {...}` (or `async function`), by matching braces."""
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\(", src)
    assert m, name
    # the parameter list may hold defaults with braces (o = {}): the body starts after its closing parenthesis
    depth, j = 0, m.end() - 1
    while True:
        c = src[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    i = src.index("{", j)
    depth = 0
    for k in range(i, len(src)):
        c = src[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():k + 1]
    raise AssertionError(name)


def js_line(src, start):
    """The whole line that starts with `start`."""
    m = re.search(r"^" + re.escape(start) + r".*$", src, re.M)
    assert m, start
    return m.group(0)


# A small DOM: elements with classes, ids, data attributes, a tiny HTML parser for innerHTML, querySelector for simple
# selectors (tag, #id, .class, [attr], [attr="v"], [attr^="v"], lists), closest, contains, bubbling click and keydown
# listeners on window (capture first).
DOM = r"""
const noop = () => {};
const camel = s => s.replace(/-([a-z])/g, (m, c) => c.toUpperCase());
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parentElement = null; this.attrs = {}; this.dataset = {};
    this._text = ''; this.ls = {}; this.style = { setProperty: noop, removeProperty: noop }; this.className = ''; this.disabled = false;
    const me = this; this.classList = { add: (...c) => { me.className = [...new Set([...me.cls(), ...c])].join(' '); },
      remove: (...c) => { me.className = me.cls().filter(x => !c.includes(x)).join(' '); }, contains: c => me.cls().includes(c),
      toggle: (c, f) => { const on = f === undefined ? !me.cls().includes(c) : !!f; on ? me.classList.add(c) : me.classList.remove(c); return on; } }; }
  cls() { return this.className.split(/\s+/).filter(Boolean); }
  get id() { return this.attrs.id || ''; }
  setAttribute(k, v) { v = String(v); this.attrs[k] = v; if (k === 'class') this.className = v; if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = v; if (k === 'disabled') this.disabled = true; }
  getAttribute(k) { if (k === 'class') return this.className; if (k.startsWith('data-')) { const d = this.dataset[camel(k.slice(5))]; return d === undefined ? null : d; } return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return this.getAttribute(k) !== null; }
  removeAttribute(k) { delete this.attrs[k]; if (k.startsWith('data-')) delete this.dataset[camel(k.slice(5))]; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  get isConnected() { let n = this; while (n.parentElement) n = n.parentElement; return n === document.documentElement; }
  appendChild(c) { if (c.parentElement) c.remove(); c.parentElement = this; this.children.push(c); return c; }
  remove() { const p = this.parentElement; if (p) { p.children = p.children.filter(x => x !== this); this.parentElement = null; } }
  contains(n) { for (; n; n = n.parentElement) if (n === this) return true; return false; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.children.forEach(c => { c.parentElement = null; }); this.children = []; this._text = String(v); }
  set innerHTML(h) { this.textContent = ''; parseInto(this, String(h)); }
  matches(sel) { return sel.split(',').some(s => { const parts = s.trim().split(/\s+/); if (!matchOne(this, parts.pop())) return false;
    let n = this.parentElement; while (parts.length && n) { if (n.matches && matchOne(n, parts[parts.length - 1])) parts.pop(); n = n.parentElement; } return !parts.length; }); }
  closest(sel) { for (let n = this; n && n.matches; n = n.parentElement) if (n.matches(sel)) return n; return null; }
  querySelectorAll(sel) { const out = []; const walk = n => n.children.forEach(c => { if (c.matches(sel)) out.push(c); walk(c); }); walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(t, f) { (this.ls[t] ||= []).push(f); }
  removeEventListener(t, f) { if (this.ls[t]) this.ls[t] = this.ls[t].filter(x => x !== f); }
  click() { if (this.disabled) return; const e = { type: 'click', target: this, preventDefault: noop, stopPropagation: noop };
    for (let n = this; n; n = n.parentElement) { e.currentTarget = n; (n.ls.click || []).slice().forEach(f => f(e)); if (n.onclick) n.onclick(e); } }
  focus() { document.activeElement = this; }
  animate() { return { finished: Promise.resolve(), cancel: noop }; }
  getAnimations() { return []; }
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  get offsetHeight() { return 0; }
}
function matchOne(el, s) {
  const re = /(^[a-z0-9]+)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:(\^?=)"?([^"\]]*)"?)?\]/gi; let m, any = false, pos = 0;
  while ((m = re.exec(s))) {
    if (m.index !== pos) return false; pos = re.lastIndex; any = true;
    if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
    if (m[2] && el.id !== m[2]) return false;
    if (m[3] && !el.cls().includes(m[3])) return false;
    if (m[4]) { const v = el.getAttribute(m[4]); if (v === null) return false; if (m[5] === '=' && v !== m[6]) return false; if (m[5] === '^=' && !v.startsWith(m[6])) return false; }
  }
  return any && pos === s.length;
}
function parseInto(root, h) {
  const re = /<\/?([a-zA-Z0-9]+)([^>]*)>|([^<]+)/g; let m, cur = root;
  while ((m = re.exec(h))) {
    if (m[3] !== undefined) { const t = new El('#text'); t._text = m[3]; t.matches = () => false; cur.appendChild(t); continue; }
    if (m[0][1] === '/') { cur = cur.parentElement || root; continue; }
    const el = new El(m[1]); const ar = /([\w-]+)(?:="([^"]*)")?/g; let a;
    while ((a = ar.exec(m[2]))) el.setAttribute(a[1], a[2] === undefined ? '' : a[2]);
    cur.appendChild(el); if (!/^(br|img|input|path|circle)$/i.test(m[1]) && !m[2].trim().endsWith('/')) cur = el;
  }
}
const winLs = { capture: [], bubble: [] };
globalThis.window = globalThis;
globalThis.document = { documentElement: new El('html'), activeElement: null, addEventListener: (t, f, c) => globalThis.addEventListener(t, f, c),
  removeEventListener: (t, f, c) => globalThis.removeEventListener(t, f, c), createElement: t => new El(t) };
document.body = document.documentElement.appendChild(new El('body'));
document.activeElement = document.body;
globalThis.addEventListener = (t, f, c) => { if (t === 'keydown') winLs[c === true || (c && c.capture) ? 'capture' : 'bubble'].push(f); };
globalThis.removeEventListener = (t, f, c) => { if (t === 'keydown') for (const k of ['capture', 'bubble']) winLs[k] = winLs[k].filter(x => x !== f); };
/* a key pressed on the focused element: the capture listeners, then (unless stopped) the bubbling ones */
function key(k, o = {}) { let stop = false; const e = { key: k, repeat: false, ...o, target: document.activeElement, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stop = true; }, stopImmediatePropagation() { stop = true; } };
  for (const f of winLs.capture.slice()) { if (stop) break; f(e); }
  for (const f of winLs.bubble.slice()) { if (stop) break; f(e); }
  return e; }
const $ = (s, r = document.documentElement) => r.querySelector(s);
const $$ = (s, r = document.documentElement) => r.querySelectorAll(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const t = (s, v) => v ? s.replace(/\{(\w+)\}/g, (m, k) => k in v ? v[k] : m) : s;
const anim = () => ({ finished: Promise.resolve(), cancel: noop });
const EASE = {}, ic = n => '', tick = ms => new Promise(r => setTimeout(r, ms));
const sounds = [];
globalThis.SH = { sound: e => sounds.push(e), t };
const out = x => { process.stdout.write(JSON.stringify(x)); process.exit(0); };
"""


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Dialogs(unittest.TestCase):
    def run_js(self, code):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        src = os.path.join(d, "t.js")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(DOM + "(async () => {\n" + code + "\n})().catch(e => { console.log(String(e && e.stack || e)); process.exit(3); });")
        r = subprocess.run(["node", src], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def test_a_refreshed_toast_undoes_the_latest_item(self):
        shell = read("js", "shell.js")
        toast = "\n".join([js_line(shell, "const ACCESS_DENIED = "), js_function(shell, "toastText"), js_function(shell, "toast")])
        out = self.run_js(r"""
          const box = document.body.appendChild(new El('div')); box.setAttribute('id', 'toasts');
          const placeToasts = noop, fitToasts = noop; let toastRoom = { at: 'side' };
          """ + toast + r"""
          const undone = [];
          toast('Result deleted.', { action: { label: 'Undo', run: () => undone.push('first') } });
          toast('Result deleted.', { action: { label: 'Undo', run: () => undone.push('second') } });
          const n = box.children.length, btns = $$('button', box).length;
          $('button', box).click();
          /* a refresh without an action takes the old button away; one with an action brings a button back */
          toast('Saved.'); toast('Saved.', { action: { label: 'View', run: () => undone.push('view') } });
          const saved = box.children[1], label = $('button', saved).textContent; $('button', saved).click();
          toast('Moved.', { action: { label: 'Undo', run: noop } }); toast('Moved.');
          const gone = $('button', box.children[2]) === null;
          /* a raw "access denied" reads as a sentence; the technical part is a second line, the whole text the tooltip */
          const raw = 'Nothing was fixed: HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\TabTip.exe: [WinError 5] Acceso denegado';
          toast(raw, { kind: 'error' }); const d = box.children[3];
          out({ n, btns, undone, label, gone, main: $('.tm', d).textContent, tech: $('.tt', d).textContent, title: d.firstElementChild.title === raw });
        """)
        self.assertEqual(out["main"], "Nothing was fixed. Windows denied access.")
        self.assertTrue(out["tech"].startswith("HKLM\\SOFTWARE") and out["tech"].endswith("Acceso denegado"))
        self.assertTrue(out["title"])
        self.assertEqual(out["n"], 1)                       # still one toast
        self.assertEqual(out["btns"], 1)                    # with one button
        self.assertEqual(out["undone"], ["second", "view"])  # the latest item, not the first
        self.assertEqual(out["label"], "View")
        self.assertTrue(out["gone"])

    def test_driver_confirmation_honours_cancel(self):
        ask = js_function(read("js", "modules", "drivers.js"), "ask")
        out = self.run_js(r"""
          const v = document.body.appendChild(new El('section')); v.setAttribute('id', 'v-drivers');
          """ + ask + r"""
          const res = {};
          /* Enter with the focus on the confirm button: yes */
          let p = ask('Disable the device', 'x', 'Disable', true); await tick(40); key('Enter'); res.enter = await p;
          /* Tab to Cancel, Enter: no */
          p = ask('Disable the device', 'x', 'Disable', true); await tick(40); $('[data-n]', $('.dq-o', v)).focus(); key('Enter'); res.cancel = await p;
          /* a held Enter (auto-repeat) does nothing; Esc then cancels */
          p = ask('Disable the device', 'x', 'Disable', true); await tick(40); key('Enter', { repeat: true }); let early = 'open';
          p.then(x => { early = x; }); await tick(10); res.repeat = early; key('Escape'); res.esc = await p;
          /* a double click on Install asks once: the second question answers no at once, and one Enter says yes once */
          await tick(10); const p1 = ask('Install 2 drivers', 'x', 'Install'), p2 = ask('Install 2 drivers', 'x', 'Install');
          res.second = await p2; res.dialogs = $$('.dq-o', v).length; await tick(40); key('Enter'); res.first = await p1;
          out(res);
        """)
        self.assertEqual(out, {"enter": True, "cancel": False, "repeat": "open", "esc": False,
                               "second": False, "dialogs": 1, "first": True})

    def test_update_confirmation_closes_with_escape_and_queues_once(self):
        src = read("js", "modules", "updater.js")
        confirm = js_function(src, "confirmRun")
        out = self.run_js(r"""
          const ID = 'updater', REAL = true, state = { view: 'updater', admin: true };
          const S = { byId: { a: { id: 'a', name: 'A' }, b: { id: 'b', name: 'B' } } }; const tile = () => '', vers = () => '';
          const v = document.body.appendChild(new El('section')); v.setAttribute('id', 'v-updater');
          let homeAt = 0; globalThis.addEventListener('keydown', e => { if (e.key === 'Escape' && !e.defaultPrevented) homeAt++; });
          """ + confirm + r"""
          let runs = 0;
          confirmRun(['a', 'b'], 'Update all', () => runs++);
          const opened = !!$('.u-confirm', v);
          key('Escape'); await tick(10);
          const afterEsc = { open: !!$('.u-confirm', v), home: homeAt, runs };
          confirmRun(['a', 'b'], 'Update all', () => runs++);
          const yes = $('[data-cyes]', $('.u-confirm', v)); yes.click(); yes.click(); await tick(10);
          /* gone: Esc again belongs to the page (Home) */
          key('Escape');
          out({ opened, afterEsc, runs, open: !!$('.u-confirm', v), home: homeAt });
        """)
        self.assertTrue(out["opened"])
        self.assertEqual(out["afterEsc"], {"open": False, "home": 0, "runs": 0})
        self.assertEqual(out["runs"], 1)
        self.assertFalse(out["open"])
        self.assertEqual(out["home"], 1)

    def test_a_double_click_answers_a_dialog_once(self):
        hangar = js_function(read("js", "modules", "hangar.js"), "dialog")
        monitor = js_function(read("js", "modules", "monitor.js"), "confirmDlg")
        out = self.run_js(r"""
          const ID = 'hangar', _t = t;
          const hv = document.body.appendChild(new El('section')); hv.setAttribute('id', 'v-hangar');
          const mv = document.body.appendChild(new El('section')); mv.setAttribute('id', 'v-monitor');
          """ + hangar + "\n" + monitor + r"""
          let installs = 0, ends = 0;
          dialog('<h3>Install 2 apps?</h3><button class="btn" data-no>Not now</button><button class="btn pri" data-yes>Install</button>',
            (e, close) => { if (!e.target.closest('[data-yes]')) return; close(); installs++; });
          const y = $('[data-yes]', $('.hg-dlg', hv)); y.click(); y.click();
          confirmDlg({ title: 'End x', text: 'y', yes: 'End task' }, () => ends++);
          const e = $('[data-d="yes"]', $('.mdlg', mv)); e.click(); e.click();
          out({ installs, ends });
        """)
        self.assertEqual(out, {"installs": 1, "ends": 1})

    def test_the_privacy_plan_runs_once_and_cancel_stops_it(self):
        src = read("js", "modules", "privacy.js")
        self.assertIn("if (e.target.closest('#pvFixAll')) { if (!planRun) showPlan(); return; }", src)
        self.assertIn("if (planRun) { planRun.stop = true; no.disabled = true; return; }", src)
        self.assertIn("if (e.target.closest('#pvPlanGo')) { if (!planRun) runPlan(); return; }", src)
        self.assertRegex(js_function(src, "runPlan"), r"for \(const f of fs\) \{ if \(run\.stop\) break;[\s\S]*finally \{ planRun = null; \}")

    def test_self_update_click_that_joins_a_background_download_still_installs(self):
        start = js_function(read("js", "selfupdate.js"), "start")
        out = self.run_js(r"""
          let running = false, stagedVer = null; const WHY = {}, mb = n => n + ' B';
          let box = document.body.appendChild(new El('div')); box.innerHTML = '<span id="suNow"></span><span id="suHint"></span>';
          const log = [], progressView = noop, stepTo = noop, bar = noop, paintSettings = noop;
          const failView = x => { log.push('fail:' + x); running = false; }, finish = v => log.push('finish:' + v), close = () => { log.push('close'); box = null; };
          SH.toast = x => log.push('toast:' + x);
          /* the engine: Update joins the update task's background run (state from its own start), which ends "staged" */
          let script = [], starts = 0;
          SH.engine = async (m, fn) => { if (fn === 'start') { starts++; return { ok: true, mode: 'task', at: starts * 100 }; }
            return script.length > 1 ? script.shift() : script[0]; };
          """ + start + r"""
          const run = async states => { script = states; await start(); for (let i = 0; i < 40 && running && !log.length; i++) await tick(200); return log.slice(); };
          script = [{ st: 'download', ts: 101, got: 1, total: 2 }, { st: 'staged', ts: 102, ver: '2.2.0' }];
          /* after the second start the updater reuses the file and says ready */
          const orig = SH.engine; SH.engine = async (m, fn) => { const r = await orig(m, fn); if (fn === 'start' && starts === 2) script = [{ st: 'checking', ts: 201 }, { st: 'ready', ts: 202, ver: '2.2.0' }]; return r; };
          const first = await run(script);
          /* staged twice: no endless loop and no "stopped answering": it says it installs at the next start */
          log.length = 0; running = false; starts = 0; box = document.body.appendChild(new El('div')); box.innerHTML = '<span id="suNow"></span><span id="suHint"></span>';
          SH.engine = async (m, fn) => { if (fn === 'start') { starts++; return { ok: true, mode: 'task', at: starts * 100 }; } return { st: 'staged', ts: starts * 100 + 1, ver: '2.2.0' }; };
          await start(); for (let i = 0; i < 40 && running; i++) await tick(200);
          out({ first, second: log, starts, stagedVer });
        """)
        self.assertEqual(out["first"], ["finish:2.2.0"])
        self.assertEqual(out["starts"], 2)
        self.assertEqual(out["second"][0], "close")
        self.assertTrue(out["second"][1].startswith("toast:RsHub 2.2.0 is ready"))
        self.assertEqual(out["stagedVer"], "2.2.0")

    def test_a_privacy_change_that_failed_is_an_error(self):
        src = read("js", "modules", "privacy.js")
        out = self.run_js(r"""
          const state = {}; SH.requireAdmin = noop; let answer;
          SH.engine = async () => answer;
          """ + js_function(src, "eng") + r"""
          const res = [];
          for (const a of [{ ok: false, error: 'Windows said no' }, { error: 'x' }, undefined, null, { needAdmin: true }, { ok: true, backup: 'b', label: 'Done' }]) {
            answer = a; try { const r = await eng('fix', 'uac'); res.push('ok:' + r.label); } catch (e) { res.push(e.needAdmin ? 'admin' : 'err:' + e.message); } }
          out(res);
        """)
        self.assertEqual(out, ["err:Windows said no", "err:x", "err:Something went wrong.", "err:Something went wrong.", "admin", "ok:Done"])
        self.assertIn("r && r.backup ? { kind: 'success', action:", src)   # Undo only with a backup

    def test_emptying_the_quarantine_waits_for_the_engine(self):
        src = read("js", "modules", "remover.js")
        # the list is emptied only once the engine said so; on its error the sessions are read again
        self.assertRegex(src, r"SH\.engine\('remover', 'quarantine_empty'\)\.then\(\(\) => \{ b\.disabled = false; emptied\(n\); \},\s*"
                              r"err => \{ b\.disabled = false; SH\.toast\(jobErr\(err\), \{ ms: 7000, kind: 'error' \}\); loadRealSessions\(\)")
        self.assertIn("realInfo && realInfo.purgeKept", src)

    def test_the_update_queue_reports_each_run_alone(self):
        fin = js_function(read("js", "modules", "updater.js"), "finishedToast")
        out = self.run_js(r"""
          const toasts = []; SH.toast = x => toasts.push(x); const S = {};
          const Q = { list: [{ st: 'done' }, { st: 'done' }, { st: 'fail' }], act: null };
          """ + fin + r"""
          finishedToast();
          Q.list.push({ st: 'done' });   /* a second run with one more update */
          finishedToast();
          out(toasts);
        """)
        self.assertEqual(out, ["2 updates installed. 1 failed.", "1 update installed."])

    def test_a_reused_confirmation_is_not_hidden_by_the_last_fade(self):
        monitor = js_function(read("js", "modules", "monitor.js"), "confirmDlg")
        out = self.run_js(r"""
          const _t = t; const mv = document.body.appendChild(new El('section')); mv.setAttribute('id', 'v-monitor');
          """ + monitor + r"""
          confirmDlg({ title: 'End a', text: 'x', yes: 'End task' }, noop);
          const dlg = $('.mdlg', mv); $('[data-d="no"]', dlg).click();          /* its fade ends on a later tick */
          confirmDlg({ title: 'End b', text: 'x', yes: 'End task' }, noop);
          await tick(20);
          out(dlg.classList.contains('on'));
        """)
        self.assertTrue(out)

    def test_a_new_footer_ignores_the_second_click_of_a_double_click(self):
        src = read("js", "modules", "remover.js")
        self.assertIn("function setFoot(html) { foot.innerHTML = html; footAt = performance.now();", src)
        self.assertIn("foot.addEventListener('click', e => { if (performance.now() - footAt < 400) { e.preventDefault(); e.stopImmediatePropagation(); } }, true);", src)

    def test_toasts_never_go_to_the_top_bar(self):
        place = js_function(read("js", "shell.js"), "placeToasts")
        self.assertNotIn("'top'", place)                       # beside the dock, or (no room at all) just above it
        self.assertIn("at = 'side'", place)
        self.assertIn("at = 'above'", place)
        css = read("css", "base.css")
        self.assertNotIn(".toasts[data-at=top]", css)
        self.assertRegex(css, r"html\[data-toasts=above\]\{--dockspace:\d+px\}")   # the strip above the dock is kept free

    def test_report_a_bug(self):
        shell = read("js", "shell.js")
        self.assertIn("const BUG_URL = 'https://github.com/Ruiz-RS/RsHub/issues/new';", shell)
        self.assertIn("SH.call('open_url', BUG_URL) === undefined) window.open(BUG_URL)", shell)
        self.assertRegex(read("index.html"), r'<button class="bug-btn" id="bugBtn" type="button" aria-label="Report a bug" title="Report a bug"[^>]*data-t-attr="aria-label title"')
        for f, pat in (("gamepad.js", r"const OVERLAYS = \[(.*?)\];"), ("gamepad.js", r"const MODALS = \[(.*?)\];"),
                       ("sounds.js", r"const MODALS = '(.*?)';")):
            self.assertIn(".bug-pop", re.search(pat, read("js", f)).group(1), f)
        self.assertIn(".ver-tag", re.search(r"const FLOATING = '(.*?)';", read("js", "gamepad.js")).group(1))
        css = read("css", "base.css")
        self.assertRegex(css, r"\.ver-tag\{[^}]*pointer-events:none")
        self.assertRegex(css, r"\.bug-btn\{pointer-events:auto")

    def test_update_confirmation_is_a_known_dialog(self):
        for f, pat in (("gamepad.js", r"const OVERLAYS = \[(.*?)\];"), ("gamepad.js", r"const MODALS = \[(.*?)\];"),
                       ("sounds.js", r"const MODALS = '(.*?)';")):
            m = re.search(pat, read("js", f))
            self.assertIn(".u-confirm", m.group(1), f)

    def test_duplicate_retry_skips_what_is_already_in_the_bin(self):
        src = read("js", "modules", "storage.js")
        code = "\n".join([js_line(src, "const copyPath = "), js_line(src, "const dupPaths = "), js_function(src, "cleanGroups")])
        out = self.run_js(r"""
          const ID = 'storage', T = t, fmtB = n => n + ' B', finished = [], toasts = [];
          SH.toast = (x, o) => toasts.push(x); SH.logActivity = noop;
          const finishGroup = d => { d.done = true; finished.push(d.id); }, updDups = noop, fromTrash = async () => ({ ok: true });
          let busy = new Set(['C:\\b\\f.iso']), trashed = [];
          const toTrash = async p => { if (busy.has(p)) return { ok: false, error: 'in use' }; if (trashed.includes(p)) return { ok: false, error: 'not found' }; trashed.push(p); return { ok: true }; };
          """ + code + r"""
          const d = { id: 7, name: 'f.iso', size: 10, keep: 0, done: false, copies: [['C:\\a', '', 'C:\\a\\f.iso'], ['C:\\b', '', 'C:\\b\\f.iso'], ['C:\\c', '', 'C:\\c\\f.iso']] };
          await cleanGroups([d], null);
          const first = { finished: finished.slice(), trashed: trashed.slice() };
          busy.clear(); toasts.length = 0;
          await cleanGroups([d], null);   /* the retry: only the copy that was in use */
          out({ first, finished, trashed, toasts });
        """)
        self.assertEqual(out["first"], {"finished": [], "trashed": ["C:\\c\\f.iso"]})
        self.assertEqual(out["trashed"], ["C:\\c\\f.iso", "C:\\b\\f.iso"])
        self.assertEqual(out["finished"], [7])
        self.assertFalse([x for x in out["toasts"] if "couldn't" in x])   # nothing failed on the retry

    def test_a_copy_in_the_bin_cannot_be_the_one_kept(self):
        src = read("js", "modules", "storage.js")
        self.assertRegex(src, r"d\.gone && d\.gone\.has\(copyPath\(d, d\.copies\[j\]\)\)\) \{ SH\.sound\('unavailable'\); return; \}\s*d\.keep = j;")

    def test_the_leftover_review_answers_once(self):
        src = read("js", "modules", "remover.js")
        self.assertIn("keep.onclick = () => { if (answer()) stepDone([]); };", src)
        self.assertIn("if (del) del.onclick = async () => { if (!answer()) return;", src)
        self.assertIn("b.disabled = !s.length || answered;", src)


    def test_a_dns_change_that_failed_half_way_offers_undo(self):
        src = read("js", "modules", "monitor.js")
        start = src.index("  seg.addEventListener('click', e => {\n    const b = e.target.closest('button'); if (!b || b.classList.contains('on')) return;\n    const again = { action: 'monitor.dns'")
        i, depth = src.index("{", src.index("=>", start)), 0
        for k in range(i, len(src)):
            depth += {"{": 1, "}": -1}.get(src[k], 0)
            if depth == 0:
                break
        handler = src[start:k + 1] + ");"
        out = self.run_js(r"""
          const _t = t, el = document.body.appendChild(new El('section'));
          el.innerHTML = '<div class="seg"><button data-v="auto" class="on">Auto</button><button data-v="cf">Cloudflare</button></div><span id="nDnsTxt">Automatic</span>';
          const seg = $('.seg', el), S = (r, id) => $('#' + id, r), DNSN = { auto: 'Automatic', cf: 'Cloudflare' }, adapter = { alias: 'Wi-Fi' };
          const cache = { net: 1 }, REAL = () => true, setSeg = noop, calls = [], toasts = [];
          const eng = async (fn, ...a) => { calls.push([fn, ...a]); return fn === 'set_dns' ? { ok: false, error: 'Only IPv4 was changed', backup: 'b1' } : { ok: true }; };
          const confirmDlg = (o, yes) => yes();
          Object.assign(SH, { requireAdmin: () => true, placeSeg: noop, logActivity: noop, toast: (m, o = {}) => toasts.push([m, o]) });
          """ + handler + r"""
          $('[data-v="cf"]', seg).click(); await tick(5);
          const [msg, o] = toasts[0], on = $('.on', seg).dataset.v, cached = 'net' in cache;
          await o.action.run(); await tick(5);
          out({ msg, kind: o.kind, label: o.action.label, on, cached, calls, last: toasts[toasts.length - 1][0] });
        """)
        self.assertEqual(out["msg"], "Could not change the DNS: Only IPv4 was changed")
        self.assertEqual(out["kind"], "error")
        self.assertEqual(out["label"], "Undo")
        self.assertEqual(out["on"], "auto")             # the choice shown goes back to what it was
        self.assertFalse(out["cached"])                 # and the network panel reads the adapter again
        self.assertEqual(out["calls"], [["set_dns", "Wi-Fi", "cf"], ["dns_undo", "b1"]])
        self.assertEqual(out["last"], "DNS restored.")

    def test_the_quiet_download_rechecks_the_settings_when_its_wait_ends(self):
        src = read("js", "selfupdate.js")
        parts = "\n".join([js_line(src, "const QUIET = "), js_line(src, "const STAGE_AFTER = "), js_line(src, "let staging = null"),
                           js_line(src, "const quiet = "), js_function(src, "stage"), js_function(src, "stageRun"), js_function(src, "check")])
        out = self.run_js(r"""
          let box = null, running = false, last = null; const log = [], paintSettings = noop;
          const state = { settings: { autoUpdate: true, autoUpdateFeatures: true } };
          const kindOf = (a, b) => a.split('.')[1] !== b.split('.')[1] ? 'minor' : 'patch';
          const ask = async () => ({ ok: true, newer: true, current: '2.1.1', latest: '2.2.0', mode: 'direct' });
          const open = () => log.push('open');
          """ + parts + r"""
          /* the ten-minute wait: kept aside and run on demand */
          const waits = new Map(); let wid = 1e6; const realST = setTimeout, realCT = clearTimeout;
          globalThis.setTimeout = (f, ms) => { if (ms < 60000) return realST(f, ms); waits.set(++wid, f); return wid; };
          globalThis.clearTimeout = id => { waits.delete(id); realCT(id); };
          const runWaits = () => { const fs = [...waits.values()]; waits.clear(); fs.forEach(f => f()); };
          let prefetches = 0;
          let fetched = false;
          SH.engine = async (m, fn) => { if (fn === 'prefetch') { prefetches++; fetched = true; await tick(5); return { ok: true, at: 1 }; } return fetched ? { st: 'staged', ts: 2, ver: '2.2.0' } : { st: 'idle' }; };
          const r = {};
          /* checked twice during the wait: one wait, one download */
          await check(false); await check(false); r.waits = waits.size;
          runWaits(); await tick(3200); r.once = prefetches;
          /* the version skipped, then new features turned off, during the wait: nothing is downloaded */
          stagedVer = null; prefetches = 0; fetched = false;
          await check(false); state.settings.skipVersion = '2.2.0'; runWaits(); await tick(30); r.skipped = prefetches;
          delete state.settings.skipVersion; await check(false); state.settings.autoUpdateFeatures = false; runWaits(); await tick(30); r.off = prefetches;
          out(r);
        """)
        self.assertEqual(out, {"waits": 1, "once": 1, "skipped": 0, "off": 0})

if __name__ == "__main__":
    unittest.main()

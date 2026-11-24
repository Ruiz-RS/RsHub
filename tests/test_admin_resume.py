"""The administrator prompt and the resume after the relaunch (rshub/resume.py, js/admin-resume.js).

Python: the ticket the normal copy leaves for the elevated one is checked field by field, valid for two minutes, read
once and deleted through the handle it was read from (never through a link or a junction), only honoured with the
hash the new copy got on its command line, and only for the actions in resume.ACTIONS, which must be the very list the
UI registers. Node: the dialog (role, focus, Esc / Not now, Windows saying no), the ticket's shape, and that the
registered actions check what they act on against the module's current state before clicking anything.
Nothing here elevates, relaunches or touches the system: elevation and the relaunch are mocks, files go to a temp folder."""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from rshub import config, elevation, resume
from rshub.api import Api

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "rshub", "ui")


def good(**kw):
    t = {"v": 1, "at": time.time(), "module": "drivers", "tab": "updates", "settings": "", "home": "bento",
         "scroll": [{"k": "#daScroll", "y": 180}, {"k": "tab", "y": 40}], "fields": [{"k": "#daQ", "v": "Intel"}],
         "action": "drivers.install", "args": {"ids": ["u1", "u2"]}}
    t.update(kw)
    return t


class Validate(unittest.TestCase):
    def test_a_good_ticket_passes_as_it_is(self):
        t = resume.validate(good())
        self.assertEqual(t["action"], "drivers.install")
        self.assertEqual(t["args"], {"ids": ["u1", "u2"]})
        self.assertEqual(t["fields"], [{"k": "#daQ", "v": "Intel"}])

    def test_only_listed_actions(self):
        self.assertIsNone(resume.validate(good(action="drivers.formatDisk")))
        self.assertIsNone(resume.validate(good(action="eval")))
        self.assertIsNone(resume.validate(good(action="engine.call")))
        self.assertEqual(resume.validate(good(action="", args={"x": 1}))["args"], {})

    def test_expired_or_from_the_future(self):
        self.assertIsNone(resume.validate(good(at=time.time() - resume.MAX_AGE - 1)))
        self.assertIsNone(resume.validate(good(at=time.time() + 60)))
        self.assertIsNone(resume.validate(good(at="now")))
        self.assertIsNone(resume.validate(good(v=2)))

    def test_places_are_plain_ids(self):
        self.assertIsNone(resume.validate(good(module="Drivers")))
        self.assertIsNone(resume.validate(good(tab="up dates")))
        self.assertIsNone(resume.validate(good(scroll=[{"k": "body *", "y": 1}])))
        self.assertIsNone(resume.validate(good(scroll=[{"k": "#a", "y": -1}])))
        self.assertIsNone(resume.validate(good(fields=[{"k": "tab", "v": "x"}])))  # a field is always an #id
        self.assertIsNone(resume.validate(good(fields=[{"k": "#q", "v": "x" * 201}])))

    def test_args_stay_small_and_plain(self):
        self.assertIsNone(resume.validate(good(args={"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}})))
        self.assertIsNone(resume.validate(good(args={"s": "x" * 401})))
        self.assertIsNone(resume.validate(good(args={"l": list(range(301))})))
        self.assertIsNone(resume.validate(good(args={"bad key": 1})))
        self.assertIsNone(resume.validate(good(args={"n": float("nan")})))
        self.assertIsNone(resume.validate(good(args=["u1"])))

    def test_unknown_fields_are_dropped(self):
        t = resume.validate(good(run="SH.engine('x')", code="alert(1)"))
        self.assertNotIn("run", t)
        self.assertNotIn("code", t)


class Ticket(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(config, "DATA_DIR", self.d)
        p.start()
        self.addCleanup(p.stop)
        resume._taken = False
        self.addCleanup(setattr, resume, "_taken", False)

    def test_save_and_take_once(self):
        token = resume.save(good())
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        with open(resume.path(), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), token)
        t = resume.take(token)
        self.assertEqual(t["action"], "drivers.install")
        self.assertFalse(os.path.exists(resume.path()))  # read once, deleted
        resume.save(good())
        self.assertIsNone(resume.take(token))  # a second take in the same copy gets nothing

    def test_a_short_spelling_of_the_folder_is_the_same_file(self):
        """GitHub's runner has TEMP as C:\\Users\\RUNNER~1\\...: the 8.3 spelling of a folder is not a link."""
        import ctypes
        long_dir = os.path.join(self.d, "a folder with a long name")
        os.makedirs(long_dir)
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(long_dir, buf, 1024)
        short = buf.value[:n] if 0 < n < 1024 else ""
        if not short or os.path.normcase(short) == os.path.normcase(long_dir):
            self.skipTest("no 8.3 names on this drive")
        with mock.patch.object(config, "DATA_DIR", short):
            token = resume.save(good())
            self.assertEqual(resume.take(token)["action"], "drivers.install")
            self.assertFalse(os.path.exists(resume.path()))

    def test_the_normal_copy_stamps_the_time_itself(self):
        resume.save(good(at=1))  # whatever the page said
        with open(resume.path(), encoding="ascii") as fh:
            self.assertGreater(json.load(fh)["at"], time.time() - 5)

    def test_an_invalid_ticket_is_not_written(self):
        self.assertIsNone(resume.save(good(action="drivers.formatDisk")))
        self.assertFalse(os.path.exists(resume.path()))

    def test_another_file_than_the_one_on_the_command_line_is_ignored_and_deleted(self):
        token = resume.save(good())
        with open(resume.path(), "w", encoding="ascii") as fh:  # a user-level program rewrites it after the fact
            fh.write(json.dumps(good(args={"ids": ["u9"]})))
        self.assertIsNone(resume.take(token))
        self.assertFalse(os.path.exists(resume.path()))

    def test_a_bad_token_gets_nothing(self):
        resume.save(good())
        self.assertIsNone(resume.take("0" * 64))
        resume._taken = False
        resume.save(good())
        self.assertIsNone(resume.take("not a hash"))

    def test_it_expires(self):
        token = resume.save(good())
        self.assertIsNone(resume.take(token, now=time.time() + resume.MAX_AGE + 5))

    def test_a_hard_link_is_neither_read_nor_deleted(self):
        other = os.path.join(self.d, "other.json")
        with open(other, "w", encoding="ascii") as fh:
            fh.write(json.dumps(good()))
        os.link(other, resume.path())
        with open(resume.path(), "rb") as fh:
            data = fh.read()
        self.assertIsNone(resume.take(hashlib.sha256(data).hexdigest()))
        self.assertTrue(os.path.exists(other))

    def test_a_junction_on_the_way_is_neither_read_nor_deleted(self):
        real = os.path.join(self.d, "real")
        os.makedirs(real)
        with open(os.path.join(real, resume.NAME), "w", encoding="ascii") as fh:
            fh.write(json.dumps(good()))
        link = os.path.join(self.d, "link")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, real], capture_output=True)
        if r.returncode != 0:
            self.skipTest("could not make a junction here")
        with mock.patch.object(config, "DATA_DIR", link):
            with open(resume.path(), "rb") as fh:
                data = fh.read()
            self.assertIsNone(resume.take(hashlib.sha256(data).hexdigest()))
        self.assertTrue(os.path.exists(os.path.join(real, resume.NAME)))

    def test_the_token_on_the_command_line(self):
        h = "a" * 64
        self.assertEqual(resume.token_from_argv(["x", "--wait-pid", "1", "--resume", h]), h)
        self.assertIsNone(resume.token_from_argv(["x", "--resume", "zz"]))
        self.assertIsNone(resume.token_from_argv(["x"]))


class Relaunch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        for p in (mock.patch.object(config, "DATA_DIR", self.d), mock.patch("rshub.splash.show"),
                  mock.patch("rshub.splash.status"), mock.patch("rshub.splash.hand_over"),
                  mock.patch("rshub.splash.close"), mock.patch("rshub.api.main_window.config.is_admin", return_value=False),
                  mock.patch("rshub.engine.jobs.running", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        resume._taken = False
        self.addCleanup(setattr, resume, "_taken", False)

    def test_the_ticket_hash_goes_on_the_new_copy_command_line(self):
        seen = []
        api = Api()
        with mock.patch.object(elevation, "relaunch_elevated", side_effect=lambda extra=(): (seen.append(extra), True)[1]), \
                mock.patch("threading.Timer"):
            self.assertTrue(api.relaunch_admin(good()))
        self.assertEqual(seen[0][0], "--resume")
        with open(resume.path(), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), seen[0][1])

    def test_windows_says_no_nothing_is_left(self):
        api = Api()
        with mock.patch.object(elevation, "relaunch_elevated", return_value=False):
            self.assertFalse(api.relaunch_admin(good()))
        self.assertFalse(os.path.exists(resume.path()))

    def test_a_running_task_is_never_cut_off(self):
        api = Api()
        with mock.patch("rshub.engine.jobs.running", return_value=[{"name": "drivers.install"}]), \
                mock.patch.object(elevation, "relaunch_elevated") as rel:
            self.assertEqual(api.relaunch_admin(good()), "busy")
        rel.assert_not_called()
        self.assertFalse(os.path.exists(resume.path()))

    def test_only_an_elevated_copy_started_for_it_takes_the_ticket(self):
        token = resume.save(good())
        api = Api()
        with mock.patch.object(elevation.sys, "argv", ["run.py", "--resume", token]):
            self.assertIsNone(api.resume_take())  # not elevated
            with mock.patch("rshub.api.main_window.config.is_admin", return_value=True):
                self.assertEqual(api.resume_take()["args"], {"ids": ["u1", "u2"]})
        resume._taken = False
        resume.save(good())
        with mock.patch.object(elevation.sys, "argv", ["run.py"]), \
                mock.patch("rshub.api.main_window.config.is_admin", return_value=True):
            self.assertIsNone(api.resume_take())  # started some other way (the task, by hand)

    def test_the_relaunch_drops_an_old_resume_argument(self):
        calls = []
        with mock.patch.object(elevation.sys, "argv", ["run.py", "--resume", "a" * 64, "--wait-pid", "9", "--tray"]), \
                mock.patch.object(elevation.ctypes.windll.shell32, "ShellExecuteW", side_effect=lambda *a: (calls.append(a), 42)[1]):
            self.assertTrue(elevation.relaunch_elevated(("--resume", "b" * 64)))
        params = calls[0][3]
        self.assertNotIn("a" * 64, params)
        self.assertIn('"--resume" "' + "b" * 64 + '"', params)
        self.assertEqual(params.count("--wait-pid"), 1)
        self.assertEqual(calls[0][1], "runas")


class SameList(unittest.TestCase):
    def test_the_ui_registers_exactly_the_allowed_actions(self):
        found = set()
        for f in glob.glob(os.path.join(UI, "js", "**", "*.js"), recursive=True):
            with open(f, encoding="utf-8") as fh:
                found |= set(re.findall(r"SH\.resume\.register\('([^']+)'", fh.read()))
        self.assertEqual(found, set(resume.ACTIONS))

    def test_every_action_asked_for_is_registered(self):
        asked = set()
        for f in glob.glob(os.path.join(UI, "js", "**", "*.js"), recursive=True):
            with open(f, encoding="utf-8") as fh:
                asked |= set(re.findall(r"action: '([a-z]+\.[A-Za-z]+)'", fh.read()))
        self.assertTrue(asked)
        self.assertLessEqual(asked, set(resume.ACTIONS))


def read(*p):
    with open(os.path.join(UI, *p), encoding="utf-8") as fh:
        return fh.read()


def register_block(src, action):
    """The source of SH.resume.register('<action>', ...); by matching parentheses."""
    i = src.index(f"SH.resume.register('{action}'")
    depth = 0
    for k in range(i + len("SH.resume.register"), len(src)):
        c = src[k]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[i:k + 2]
    raise AssertionError(action)


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Ui(unittest.TestCase):
    def run_js(self, code):
        from tests.test_ui_dialogs import DOM
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        src = os.path.join(d, "t.js")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(DOM + "(async () => {\n" + code + "\n})().catch(e => { console.log(String(e && e.stack || e)); process.exit(3); });")
        r = subprocess.run(["node", src], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    SETUP = r"""
      globalThis.location = { search: '' }; globalThis.chrome = { webview: {} };
      const store = {}; globalThis.sessionStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } };
      const toasts = [], relaunched = [], always = [];
      let answer = false;
      const view = document.body.appendChild(new El('section')); view.setAttribute('id', 'v-drivers');
      view.innerHTML = '<nav class="subnav"><button data-tab="all">All</button><button data-tab="updates" class="on">Updates</button></nav><div class="tab on" data-tab="updates"></div>';
      Object.assign(SH, { $, $$, esc, ic, anim, EASE, state: { view: 'drivers', admin: false, setOpen: false, settings: { homeVariant: 'bento' } },
        icons: { add: noop }, toast: (text, o) => toasts.push(text), call: async () => undefined, activity: { list: () => [] },
        relaunchAdmin: async (tk, o) => { relaunched.push(tk); always.push(!!(o && o.always)); return answer; }, registry: { variants: {} }, modById: () => null, go: noop });
    """ + "\n"

    def load(self):
        return self.SETUP + read("js", "admin-resume.js") + "\n"

    def test_the_dialog(self):
        out = self.run_js(self.load() + r"""
          SH.resume.register('drivers.install', async () => true);
          const res = {};
          res.adminAlready = (SH.state.admin = true, SH.requireAdmin('to install drivers.'));
          SH.state.admin = false;
          res.ret = SH.requireAdmin('to install drivers.', { resume: { action: 'drivers.install', args: { ids: ['u1'] } } });
          await tick(60);
          const o = $('.ar-o'), d = $('.ar', o);
          res.role = d.getAttribute('role'); res.modal = d.getAttribute('aria-modal'); res.labelled = $('#' + d.getAttribute('aria-labelledby'), o) !== null;
          res.title = $('#arT', o).textContent; res.reason = $('#arD', o).textContent;
          res.focus = document.activeElement === $('[data-ar-go]', o);
          res.one = (SH.requireAdmin('to install drivers.'), $$('.ar-o').length);  /* never two at once */
          res.sound = sounds.includes('unavailable');
          key('Escape'); await tick(20); res.escClosed = !$('.ar-o');
          /* Not now (the controller's B clicks [data-close]): the work that can go on without it goes on */
          let p = SH.adminFirst('to clean Windows temporary files.'); await tick(20); $('.ar-o [data-close]').click(); res.notNow = await p;
          /* Windows says no: this copy stays as it was and says so */
          answer = false; p = SH.adminFirst('to clean Windows temporary files.'); await tick(20); $('.ar-o [data-ar-go]').click(); res.denied = await p;
          res.deniedToast = toasts.slice(-1)[0];
          /* Windows says yes: the work stops here (the elevated copy picks it up) */
          answer = true; p = SH.adminFirst('to install drivers.', { resume: { action: 'drivers.install', args: { ids: ['u1', 'u2'] } } }); await tick(20); $('.ar-o [data-ar-go]').click(); res.yes = await p;
          res.ticket = relaunched.slice(-1)[0]; res.alwaysDefault = always.slice();
          /* the box: unticked by default (this session only); ticked, the setting goes on; the per-user note shows */
          SH.state.adminTask = false;
          p = SH.adminFirst('to install drivers.'); await tick(20);
          res.boxRole = $('.ar-o [data-ar-always]').getAttribute('role'); res.noteBefore = (n => n.hidden ?? n.hasAttribute('hidden'))($('.ar-o [data-ar-note]'));
          $('.ar-o [data-ar-always]').click(); res.boxOn = $('.ar-o [data-ar-always]').getAttribute('aria-checked'); res.noteAfter = $('.ar-o [data-ar-note]').hidden;
          $('.ar-o [data-ar-go]').click(); await p; res.alwaysTicked = always.slice(-1)[0];
          /* something the user started still runs: nothing is asked */
          SH.activity.list = () => [{ user: true, bg: false }]; const before = relaunched.length;
          p = SH.adminFirst('to install drivers.'); await tick(20); $('.ar-o [data-ar-go]').click(); await p; res.busyAsked = relaunched.length !== before;
          out(res);
        """)
        self.assertTrue(out["adminAlready"])
        self.assertFalse(out["ret"])
        self.assertEqual((out["role"], out["modal"]), ("dialog", "true"))
        self.assertTrue(out["labelled"])
        self.assertEqual(out["title"], "This needs administrator mode")
        self.assertIn("to install drivers.", out["reason"])
        self.assertTrue(out["focus"])
        self.assertEqual(out["one"], 1)
        self.assertTrue(out["sound"])
        self.assertTrue(out["escClosed"])
        self.assertTrue(out["notNow"])
        self.assertTrue(out["denied"])
        self.assertEqual(out["deniedToast"], "Windows didn't give permission. You're still in normal mode.")
        self.assertFalse(out["yes"])
        t = out["ticket"]
        self.assertEqual((t["v"], t["module"], t["tab"], t["home"], t["action"]), (1, "drivers", "updates", "bento", "drivers.install"))
        self.assertEqual(t["args"], {"ids": ["u1", "u2"]})
        self.assertIsNotNone(resume.validate(dict(t, at=time.time())))  # what the page sends, Python accepts
        self.assertFalse(out["busyAsked"])
        self.assertEqual(out["alwaysDefault"], [False, False])  # Windows said no, then yes: neither made it permanent
        self.assertEqual(out["boxRole"], "checkbox")
        self.assertTrue(out["noteBefore"])
        self.assertEqual(out["boxOn"], "true")
        self.assertFalse(out["noteAfter"])
        self.assertTrue(out["alwaysTicked"])

    def test_the_relaunch_only_makes_it_permanent_when_asked(self):
        src = read("js", "shell.js")
        m = re.search(r"SH\.relaunchAdmin = async \(ticket, o = \{\}\) => \{.*?\n\};", src, re.S)
        out = self.run_js(r"""
          const state = { settings: { alwaysAdmin: false } }, sets = []; let answer = true;
          SH.call = async (name, arg) => { if (name === 'settings_set') { sets.push({ ...arg }); return true; } if (name === 'relaunch_admin') return answer; };
          """ + m.group(0) + r"""
          const res = {};
          await SH.relaunchAdmin({ v: 1 }); res.session = { sets: sets.splice(0), always: state.settings.alwaysAdmin };
          state.settings.alwaysAdmin = false; await SH.relaunchAdmin({ v: 1 }, { always: true }); res.ticked = { sets: sets.splice(0), always: state.settings.alwaysAdmin };
          state.settings.alwaysAdmin = false; answer = false; await SH.relaunchAdmin({ v: 1 }, { always: true }); res.denied = { sets: sets.splice(0), always: state.settings.alwaysAdmin };
          state.settings.alwaysAdmin = true; answer = false; await SH.relaunchAdmin({ v: 1 }); res.deniedWasOn = state.settings.alwaysAdmin;
          out(res);
        """)
        self.assertNotIn("alwaysAdmin", out["session"]["sets"][0])  # this session only
        self.assertFalse(out["session"]["always"])
        self.assertTrue(out["ticked"]["sets"][0]["alwaysAdmin"])
        self.assertTrue(out["ticked"]["always"])
        self.assertFalse(out["denied"]["always"])  # Windows said no: back as it was
        self.assertEqual(out["denied"]["sets"][-1], {"adminAsked": 0, "alwaysAdmin": False})
        self.assertTrue(out["deniedWasOn"])
        # the old Turn on paths (mode pill, note, palette, Settings button) are this session only; only the switch
        # in Settings > Administrator asks for always
        self.assertIn("SH.relaunchAdmin(SH.resume ? SH.resume.ticket() : null, { always })", src)
        self.assertEqual(len(re.findall(r"askAdmin\(true, true\)", src)), 1)
        self.assertRegex(src, r"function alwaysAdminOn\(\) \{\s+if \(!state.admin\) askAdmin\(true, true\);")

    def test_the_ticket_and_its_checks(self):
        out = self.run_js(self.load() + r"""
          SH.resume.register('drivers.install', async () => true);
          const res = {};
          res.unregistered = SH.resume.ticket({ action: 'drivers.formatDisk', args: { x: 1 } }).action;
          res.registered = SH.resume.ticket({ action: 'drivers.install', args: { ids: ['u1'] } }).action;
          const now = Date.now();
          res.fresh = !!SH.resume.valid({ v: 1, at: now, module: 'drivers', tab: 'updates', action: 'drivers.install', args: {} });
          res.seconds = !!SH.resume.valid({ v: 1, at: now / 1000, module: 'drivers' });  /* Python stamps seconds */
          res.old = SH.resume.valid({ v: 1, at: now - 200000, module: 'drivers' });
          res.unknown = SH.resume.valid({ v: 1, at: now, module: 'drivers', action: 'eval' }).action;
          res.badModule = SH.resume.valid({ v: 1, at: now, module: '<img>' }).module;
          res.badScroll = SH.resume.valid({ v: 1, at: now, scroll: [{ k: 'body *', y: 2 }, { k: '#ok', y: 3 }] }).scroll;
          let threw = false; try { SH.resume.register('Bad Id', () => 1); } catch { threw = true; } res.threw = threw;
          out(res);
        """)
        self.assertEqual(out["unregistered"], "")
        self.assertEqual(out["registered"], "drivers.install")
        self.assertTrue(out["fresh"])
        self.assertTrue(out["seconds"])
        self.assertIsNone(out["old"])
        self.assertEqual(out["unknown"], "")
        self.assertEqual(out["badModule"], "")
        self.assertEqual(out["badScroll"], [{"k": "#ok", "y": 3}])
        self.assertTrue(out["threw"])

    def test_registered_actions_check_the_current_state_first(self):
        drivers, monitor = read("js", "modules", "drivers.js"), read("js", "modules", "monitor.js")
        blocks = "\n".join([
            "const tabPanel = async (r, tab, key) => panels[tab];",
            "const rowById = (root, sel, attr, id) => $$(sel, root).find(x => x.dataset[attr] === id) || null;",
            register_block(drivers, "drivers.install"), register_block(drivers, "drivers.removeAll"),
            register_block(monitor, "monitor.dns")])
        out = self.run_js(r"""
          const fns = {};
          SH.resume = { register: (id, fn) => { fns[id] = fn; } };
          const clicks = [];
          const mk = html => { const p = document.body.appendChild(new El('div')); p.innerHTML = html; return p; };
          const up = mk('<div class="du-row" data-u="u1"></div><div class="du-row" data-u="u2"></div><button id="duInstall">Install</button>');
          let selected = null;
          up._du = { ready: () => true, has: id => ['u1', 'u2'].includes(id), select: ids => { selected = ids; } };
          $('#duInstall', up).addEventListener('click', () => clicks.push('install'));
          const pr = mk('<div id="dpG"><div class="row" data-id="g1"></div><div class="row" data-id="g2"></div></div><button id="dpAll">Remove all</button>');
          $('#dpAll', pr).addEventListener('click', () => clicks.push('removeAll'));
          const net = mk('<div class="seg" id="nDns" data-adapter="Wi-Fi"><button data-v="auto" class="on">A</button><button data-v="cloudflare">C</button></div>');
          $('[data-v="cloudflare"]', net).addEventListener('click', () => clicks.push('dns'));
          net.children[0].scrollIntoView = noop;
          const panels = { updates: up, problems: pr };
          const r = { str: (x, m = 400) => typeof x === 'string' && x.length <= m ? x : '', strs: x => Array.isArray(x) ? x.filter(v => typeof v === 'string') : [],
            wait: () => Promise.resolve(), until: async fn => fn(), open: async () => net, changed: noop };
          """ + blocks + r"""
          const res = {};
          res.gone = await fns['drivers.install']({ ids: ['u1', 'u9'] }, r); res.clicks1 = clicks.length;
          res.junk = await fns['drivers.install']({ ids: 'u1' }, r);
          res.ok = await fns['drivers.install']({ ids: ['u2'] }, r); res.selected = selected; res.clicks2 = clicks.slice();
          $('#duInstall', up).disabled = true; res.disabled = await fns['drivers.install']({ ids: ['u1'] }, r);
          res.moreNow = await fns['drivers.removeAll']({ ids: ['g1'] }, r);
          res.same = await fns['drivers.removeAll']({ ids: ['g2', 'g1'] }, r);
          res.otherAdapter = await fns['monitor.dns']({ v: 'cloudflare', adapter: 'Ethernet' }, r);
          res.notAChoice = await fns['monitor.dns']({ v: 'evil', adapter: 'Wi-Fi' }, r);
          res.already = await fns['monitor.dns']({ v: 'auto', adapter: 'Wi-Fi' }, r);
          res.dns = await fns['monitor.dns']({ v: 'cloudflare', adapter: 'Wi-Fi' }, r);
          res.clicks = clicks;
          out(res);
        """)
        self.assertFalse(out["gone"])
        self.assertEqual(out["clicks1"], 0)
        self.assertFalse(out["junk"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["selected"], ["u2"])
        self.assertEqual(out["clicks2"], ["install"])
        self.assertFalse(out["disabled"])
        self.assertFalse(out["moreNow"])  # the list changed: more devices now than were asked for
        self.assertTrue(out["same"])
        self.assertFalse(out["otherAdapter"])
        self.assertFalse(out["notAChoice"])
        self.assertFalse(out["already"])
        self.assertTrue(out["dns"])
        self.assertEqual(out["clicks"], ["install", "removeAll", "dns"])

    def test_the_dialog_is_a_known_overlay(self):
        """The focus trap, the controller (A / B) and the dialog sounds know it."""
        self.assertIn("'.ar-o'", re.search(r"const MODALS = \[[^\]]+\]", read("js", "gamepad.js")).group(0))
        self.assertIn("'.ar-o'", re.search(r"const OVERLAYS = \[[^\]]+\]", read("js", "gamepad.js")).group(0))
        self.assertIn(".ar-o", re.search(r"const MODALS = '[^']+'", read("js", "sounds.js")).group(0))
        src = read("js", "admin-resume.js")
        self.assertIn("data-close", src)  # B closes it as Not now
        self.assertIn('role="dialog" aria-modal="true"', src)

    def test_it_loads_before_the_modules_that_register(self):
        html = read("index.html")
        self.assertLess(html.index("js/admin-resume.js"), html.index("js/modules/monitor.js"))
        self.assertLess(html.index("js/shell.js"), html.index("js/admin-resume.js"))


if __name__ == "__main__":
    unittest.main()

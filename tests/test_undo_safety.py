"""Undo and safety of the Super Optimizer, Debloat, Privacy, Backups and Monitor engines: exact undo, backups written
before anything changes, no false positives in the registry cleaner, services and Windows Update restarted after a
repair. Everything runs against mocks and temporary folders (and one test-only HKCU key with a random name, always
deleted): nothing else touches the real system."""
import json
import os
import shutil
import tempfile
import unittest
import winreg
from unittest import mock

from rshub.engine import backups as B
from rshub.engine import debloat as D
from rshub.engine import monitor as M
from rshub.engine import optimizer as O
from rshub.engine import privacy as P
from rshub.engine.removal import registry
from rshub.engine.removal.registry import RegRef, V32, V64


class Job:
    cancelled = False

    def __init__(self):
        self.logs = []

    def progress(self, *a, **k):
        pass

    def log(self, line):
        self.logs.append(line)

    def check(self):
        pass

    def stop(self):
        raise RuntimeError("cancelled")


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sh_undo_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.machine = os.path.join(self.tmp, "machine")
        self.user = os.path.join(self.tmp, "user")
        for p in (mock.patch.object(O, "DATA", self.machine), mock.patch.object(O, "USER_DATA", self.user),
                  mock.patch.object(O, "_admin", return_value=True),
                  # the registry is mocked in these tests: the real keys' owner and rights are not what is tested
                  mock.patch.object(O, "_windows_protected", return_value=False)):
            p.start()
            self.addCleanup(p.stop)


class WindowsOwnedEntries(unittest.TestCase):
    """Registry cleaner on entries Windows owns (owner's bug: TabTip.exe offered under WOW6432Node, then Access denied)."""
    ENV = {"ProgramW6432": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "CommonProgramW6432": r"C:\Program Files\Common Files",
           "CommonProgramFiles(x86)": r"C:\Program Files (x86)\Common Files", "SystemRoot": r"C:\Windows"}

    def setUp(self):
        p = mock.patch.dict(os.environ, self.ENV)
        p.start()
        self.addCleanup(p.stop)

    def test_inbox_app_paths(self):
        self.assertTrue(O._inbox_app_path("TabTip.exe", r"C:\Program Files\Common Files\microsoft shared\ink\TabTip.exe"))
        self.assertTrue(O._inbox_app_path("x.exe", r"C:\Program Files (x86)\Common Files\Microsoft Shared\ink\x.exe"))
        self.assertTrue(O._inbox_app_path("wmplayer.exe", r"C:\Program Files\Windows Media Player\wmplayer.exe"))
        self.assertTrue(O._inbox_app_path("iexplore.exe", r"C:\Program Files (x86)\Internet Explorer\iexplore.exe"))
        self.assertTrue(O._inbox_app_path("x.exe", r"C:\Windows\System32\x.exe"))
        self.assertFalse(O._inbox_app_path("foo.exe", r"C:\Program Files\Foo\foo.exe"))

    def test_32_bit_entry_found_in_the_x86_common_files_is_not_missing(self):
        real = os.stat

        def fake_stat(p, *a, **k):
            if p.lower() == r"c:\program files (x86)\common files\vendor\tool.exe":
                return real(__file__)
            raise FileNotFoundError(p)
        with mock.patch.object(O, "_drive_ready", return_value=True), mock.patch("os.stat", side_effect=fake_stat):
            self.assertFalse(O._definitely_missing(r"C:\Program Files\Common Files\Vendor\tool.exe", V32))
            self.assertTrue(O._definitely_missing(r"C:\Program Files\Common Files\Vendor\gone.exe", V32))

    def test_scan_never_offers_the_touch_keyboard_or_a_protected_key(self):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        apps = {"TabTip.exe": r"C:\Program Files\Common Files\microsoft shared\ink\TabTip.exe",
                "locked.exe": r"C:\RsHubTestNowhere\locked.exe", "gone.exe": r"C:\RsHubTestNowhere\gone.exe"}

        def subkeys(root, path, view=0):
            return list(apps) if path == base and view == V32 else []

        def get_value(root, path, name, view=0, default=None):
            sub = path.rsplit("\\", 1)[-1]
            return apps.get(sub) if path.startswith(base) and name == "" and view == V32 else default
        with mock.patch.object(registry, "subkeys", subkeys), mock.patch.object(registry, "get_value", get_value), \
                mock.patch.object(registry, "values", return_value=[]), \
                mock.patch.object(O, "_drive_ready", return_value=True), \
                mock.patch.object(O, "_windows_protected", side_effect=lambda ref: ref.path.endswith("locked.exe")):
            groups = O.registry_scan(Job())
        keys = [i["k"] for g in groups for i in g["items"]]
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].endswith("gone.exe"))

    def test_protected_key_is_detected_without_changing_it(self):
        # a key of this test only (random name), removed even if the test fails
        base = r"Software\MiniRevoTest-" + __import__("uuid").uuid4().hex[:12]
        key = base + r"\Protected"
        self.addCleanup(lambda: registry.delete_tree(RegRef("HKCU", base)))
        winreg.CloseKey(winreg.CreateKey(winreg.HKEY_CURRENT_USER, key))
        ref = RegRef("HKCU", key)
        self.assertFalse(O._windows_protected(ref))                       # the user's own key
        with mock.patch.object(O, "_key_owner", return_value=O._TI_SID):
            self.assertTrue(O._windows_protected(ref))                    # owned by TrustedInstaller
        real_open = winreg.OpenKey

        def deny_write(root, path, res=0, access=winreg.KEY_READ):
            if access & winreg.KEY_SET_VALUE:
                raise PermissionError(13, "Access is denied")
            return real_open(root, path, res, access)
        with mock.patch.object(O.winreg, "OpenKey", deny_write):
            self.assertTrue(O._windows_protected(ref))                    # its rights refuse the change
        self.assertFalse(O._windows_protected(RegRef("HKCU", base + r"\NotThere")))
        self.assertTrue(registry.key_exists(ref))

class FalsePositives(unittest.TestCase):
    def test_unplugged_or_network_paths_are_not_missing(self):
        with mock.patch.object(O, "_drive_ready", return_value=False):
            self.assertFalse(O._definitely_missing(r"E:\Tools\gone.exe"))
        self.assertFalse(O._definitely_missing(r"\\server\share\x.exe"))
        self.assertFalse(O._definitely_missing(r"relative\x.exe"))
        with mock.patch.object(O, "_drive_ready", return_value=True):
            self.assertTrue(O._definitely_missing(r"C:\RsHubTestNowhere\x.exe"))

    def test_access_denied_is_not_missing(self):
        with mock.patch.object(O, "_drive_ready", return_value=True), mock.patch("os.stat", side_effect=PermissionError):
            self.assertFalse(O._definitely_missing(r"C:\Program Files\WindowsApps\x.exe"))

    def test_32_bit_entries_see_syswow64(self):
        real = os.stat

        def fake_stat(p, *a, **k):
            if p.lower() == r"c:\windows\syswow64\only32.dll":
                return real(__file__)
            raise FileNotFoundError(p)
        with mock.patch.object(O, "_drive_ready", return_value=True), mock.patch("os.stat", side_effect=fake_stat):
            dll = r"C:\Windows\System32\only32.dll"
            self.assertFalse(O._definitely_missing(dll, V32))  # a 32-bit program sees SysWOW64
            self.assertFalse(O._definitely_missing(dll, 0))    # HKCU: either one
            self.assertTrue(O._definitely_missing(dll, V64))   # a 64-bit entry means the real System32

    def test_abs_missing_ignores_unready_drives(self):
        with mock.patch.object(O, "_drive_ready", return_value=False):
            self.assertIsNone(O._abs_missing(r'"E:\Apps\app.exe" /s'))

    def test_unquoted_script_in_a_folder_with_spaces_is_not_broken(self):
        # "C:\...\Foo App\run.bat" (no .exe to anchor on) used to be read as "C:\...\Foo" and offered as broken
        tmp = tempfile.mkdtemp(prefix="sh_abs_")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "Foo App"))
        script = os.path.join(tmp, "Foo App", "run.bat")
        with open(script, "w") as fh:
            fh.write("@echo off\n")
        with mock.patch.object(O, "_drive_ready", return_value=True):
            self.assertIsNone(O._abs_missing(script))
            self.assertIsNone(O._abs_missing(script + " --tray"))
            gone = os.path.join(tmp, "Gone App", "run.bat")
            self.assertIsNotNone(O._abs_missing(gone))   # still found when really gone
            self.assertEqual(O._abs_missing(os.path.join(tmp, "Gone App", "x.exe") + " /s"),
                             os.path.join(tmp, "Gone App", "x.exe"))

    def test_hkcu_policies_need_admin(self):
        self.assertTrue(O.user_needs_admin("HKCU", r"Software\Policies\Microsoft\Windows\WindowsAI"))
        self.assertTrue(O.user_needs_admin("HKLM", r"SOFTWARE\x"))
        self.assertFalse(O.user_needs_admin("HKCU", r"Software\Microsoft\Windows"))


class RegistryFix(Tmp):
    def fix(self, ids, **patches):
        d = {"read_value_full": lambda ref: (b"\x01\x02", winreg.REG_BINARY), "delete_value": mock.DEFAULT,
             "export_key": lambda ref, f: open(f, "w").close() or True, "delete_tree": mock.DEFAULT}
        d.update(patches)
        ctx = [mock.patch.object(registry, k, **({"side_effect": v} if callable(v) else {})) for k, v in d.items()]
        mocks = [c.start() for c in ctx]
        self.addCleanup(lambda: [c.stop() for c in ctx])
        return O.registry_fix(Job(), ids), dict(zip(d, mocks))

    def test_single_value_is_saved_and_restored_exactly(self):
        run = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
        ref = RegRef("HKLM", run, V64, "Old Tool")
        r, m = self.fix([ref.to_str()])
        self.assertEqual((r["fixed"], r["failed"]), (1, []))
        m["delete_value"].assert_called_once()
        m["export_key"].assert_not_called()  # only that value: never the whole key (it would bring back others)
        with open(os.path.join(self.machine, "registry", r["backup"], "manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertEqual([(e["kind"], e["name"], e["type"], e["data"]) for e in man],
                         [("value", "Old Tool", winreg.REG_BINARY, {"hex": "0102"})])
        with mock.patch.object(registry, "set_value") as sv:
            u = O._registry_undo(r["backup"])
        self.assertTrue(u["ok"])
        args = sv.call_args[0]
        self.assertEqual((args[0], args[1], args[2]), (ref, b"\x01\x02", winreg.REG_BINARY))

    def test_access_denied_is_said_plainly_and_the_others_still_go(self):
        run = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
        locked, fine = RegRef("HKLM", run, V64, "Locked"), RegRef("HKLM", run, V64, "Old Tool")

        def delete_value(ref):
            if ref.value == "Locked":
                raise PermissionError(13, "[WinError 5] Access is denied")
        r, m = self.fix([locked.to_str(), fine.to_str()], delete_value=delete_value)
        self.assertEqual(r["fixed"], 1)
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("Windows protects this entry", r["failed"][0])
        self.assertNotIn("WinError", r["failed"][0])

    def test_a_protected_entry_is_not_touched(self):
        ref = RegRef("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\TabTip.exe", V32)
        with mock.patch.object(O, "_windows_protected", return_value=True):
            r, m = self.fix([ref.to_str()])
        self.assertEqual(r["fixed"], 0)
        self.assertIn("Windows protects this entry", r["failed"][0])
        m["export_key"].assert_not_called()
        m["delete_tree"].assert_not_called()

    def test_a_key_that_fails_halfway_is_put_back_from_its_backup(self):
        ref = RegRef("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\half.exe", V32)
        with mock.patch.object(registry, "import_reg", return_value=(True, "")) as imp:
            r, m = self.fix([ref.to_str()], delete_tree=mock.Mock(side_effect=PermissionError(13, "Access is denied")))
        self.assertEqual(r["fixed"], 0)
        imp.assert_called_once()

    def test_whole_key_uses_reg_export(self):
        ref = RegRef("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\gone.exe", V32)
        r, m = self.fix([ref.to_str()])
        self.assertEqual(r["fixed"], 1)
        m["export_key"].assert_called_once()
        m["delete_tree"].assert_called_once()
        with mock.patch.object(registry, "import_reg", return_value=(True, "")) as imp:
            self.assertTrue(O._registry_undo(r["backup"])["ok"])
        self.assertEqual(imp.call_args[0][1], V32)

    def test_ids_are_unique_even_in_the_same_second(self):
        os.makedirs(os.path.join(self.tmp, "taken-1"))
        with mock.patch.object(O, "new_bid", side_effect=["taken-1", "taken-1", "free-2"]):
            self.assertEqual(O.unique_bid(self.tmp), "free-2")  # never reuses an id that exists
        ref = RegRef("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", 0, "a").to_str()
        with mock.patch("time.strftime", return_value="20261119-120000"):
            a, _ = self.fix([ref])
            b, _ = self.fix([ref])
        self.assertNotEqual(a["backup"], b["backup"])

    def test_hkcu_backup_goes_to_the_user_profile(self):
        ref = RegRef("HKCU", r"Software\Classes\.old", 0)
        r, _ = self.fix([ref.to_str()])
        self.assertTrue(os.path.isdir(os.path.join(self.user, "registry", r["backup"])))
        self.assertFalse(os.path.exists(os.path.join(self.machine, "registry")))
        with mock.patch.object(registry, "import_reg", return_value=(True, "")):
            self.assertTrue(O._registry_undo(r["backup"])["ok"])

    def test_nothing_is_deleted_when_the_backup_cannot_be_written(self):
        ref = RegRef("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", 0, "a")
        with mock.patch.object(O, "write_json_atomic", side_effect=OSError("denied")):
            r, m = self.fix([ref.to_str(), ref.to_str()])
        m["delete_value"].assert_not_called()
        self.assertEqual(r["fixed"], 0)
        self.assertEqual(len(r["failed"]), 2)

    def test_hkcu_policies_key_needs_admin(self):
        ref = RegRef("HKCU", r"Software\Policies\Microsoft\Windows\Explorer", 0)
        with mock.patch.object(O, "_admin", return_value=False):
            self.assertEqual(O.registry_fix(Job(), [ref.to_str()]), {"needAdmin": True})


class Services(Tmp):
    ROWS = [{"Name": "WSearch", "StartMode": "Auto", "State": "Running"},
            {"Name": "Spooler", "StartMode": "Manual", "State": "Stopped"}]

    def test_backup_is_written_first_with_delayed_start(self):
        calls, seen = [], {}

        def fake_ps(cmd, timeout=90):
            files = os.listdir(os.path.join(self.machine, "services")) if os.path.isdir(os.path.join(self.machine, "services")) else []
            seen.setdefault("files_at_first_change", files)
            calls.append(cmd)
            return "ok"
        with mock.patch.object(O, "ps_json", return_value=self.ROWS), mock.patch.object(O, "ps", fake_ps), \
                mock.patch.object(O, "_delayed", side_effect=lambda n: n == "WSearch"):
            r = O.services_apply({"WSearch": "disabled", "Spooler": "disabled"})
        self.assertTrue(r["ok"])
        self.assertEqual(len(seen["files_at_first_change"]), 1)  # already on disk before the first Set-Service
        with open(os.path.join(self.machine, "services", r["backup"] + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["prev"], {"WSearch": "auto", "Spooler": "manual"})
        self.assertEqual(data["delayed"], ["WSearch"])
        self.assertEqual(data["running"], ["WSearch"])

    def test_nothing_changes_if_the_backup_cannot_be_written(self):
        with mock.patch.object(O, "ps_json", return_value=self.ROWS), mock.patch.object(O, "ps") as ps, \
                mock.patch.object(O, "write_json_atomic", side_effect=OSError("denied")), \
                mock.patch.object(O, "_delayed", return_value=False):
            r = O.services_apply({"WSearch": "disabled"})
        ps.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertIsNone(r["backup"])
        self.assertIn("error", r)

    def test_unread_service_list_is_not_a_success(self):
        # Get-CimInstance answered nothing: nothing can be changed, so it must not say "changed" (it said ok: True)
        with mock.patch.object(O, "ps_json", return_value=[]), mock.patch.object(O, "ps") as ps:
            r = O.services_apply({"WSearch": "disabled"})
        ps.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertIsNone(r["backup"])
        self.assertTrue(r.get("error"))
        self.assertFalse(os.path.isdir(os.path.join(self.machine, "services")))

    def test_two_backups_in_the_same_second_do_not_overwrite(self):
        with mock.patch.object(O, "ps_json", return_value=self.ROWS), mock.patch.object(O, "ps", return_value="ok"), \
                mock.patch.object(O, "_delayed", return_value=False), mock.patch("time.strftime", return_value="20261119-120000"):
            a = O.services_apply({"WSearch": "manual"})["backup"]
            b = O.services_apply({"WSearch": "disabled"})["backup"]
        self.assertNotEqual(a, b)
        self.assertEqual(len(os.listdir(os.path.join(self.machine, "services"))), 2)

    def test_undo_restores_delayed_auto_and_running_state(self):
        bid = "20261119-120000-abcd"
        os.makedirs(os.path.join(self.machine, "services"))
        with open(os.path.join(self.machine, "services", bid + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"t": 1, "prev": {"WSearch": "auto", "Spooler": "manual", "Fax": "manual"},
                       "delayed": ["WSearch"], "running": ["WSearch", "Spooler"]}, fh)
        runs, pscmds = [], []
        with mock.patch.object(O.rsys, "run", side_effect=lambda a, timeout=0: runs.append(a) or (0, "")), \
                mock.patch.object(O, "ps", side_effect=lambda c, timeout=0: pscmds.append(c) or "ok"):
            r = O._services_undo(bid)
        self.assertTrue(r["ok"])
        self.assertIn(["sc.exe", "config", "WSearch", "start=", "delayed-auto"], runs)
        self.assertFalse(any("WSearch" in c and "Set-Service" in c for c in pscmds))
        self.assertTrue(any("Start-Service -Name 'WSearch'" in c for c in pscmds))
        self.assertTrue(any("Start-Service -Name 'Spooler'" in c for c in pscmds))  # it was running
        self.assertFalse(any("Start-Service -Name 'Fax'" in c for c in pscmds))     # it wasn't


class Repairs(Tmp):
    def test_wu_restarts_services_when_something_fails(self):
        win = os.path.join(self.tmp, "win")
        os.makedirs(os.path.join(win, "SoftwareDistribution"))
        os.makedirs(os.path.join(win, "System32", "catroot2"))
        started = []

        def stream(job, cmd, **k):
            if cmd[:2] == ["net", "stop"] and cmd[2] == "bits":
                raise RuntimeError("cancelled")
            return 0
        with mock.patch.dict(os.environ, {"WINDIR": win}), mock.patch.object(O, "_stream", stream), \
                mock.patch.object(O, "_svc_running", return_value=True), \
                mock.patch.object(O.rsys, "run", side_effect=lambda a, timeout=0: started.append(a) or (0, "")):
            with self.assertRaises(RuntimeError):
                O.repair(Job(), "wu")
        names = [a[2] for a in started if a[:2] == ["net", "start"]]
        self.assertEqual(sorted(names), ["bits", "cryptSvc", "wuauserv"])  # every one that was touched, even bits
        self.assertTrue(os.path.isdir(os.path.join(win, "SoftwareDistribution")))  # not renamed: it stopped before

    def test_wu_keeps_only_the_newest_old_folder(self):
        win = os.path.join(self.tmp, "win2")
        sd = os.path.join(win, "SoftwareDistribution")
        os.makedirs(sd)
        os.makedirs(os.path.join(win, "System32", "catroot2"))
        for stamp in ("20260101000000", "20260601000000"):
            os.makedirs(f"{sd}.old-{stamp}")
        with mock.patch.dict(os.environ, {"WINDIR": win}), mock.patch.object(O, "_stream", return_value=0), \
                mock.patch.object(O, "_svc_running", return_value=False), mock.patch.object(O.rsys, "run", return_value=(0, "")):
            self.assertEqual(O.repair(Job(), "wu")["code"], 0)
        olds = [d for d in os.listdir(win) if d.startswith("SoftwareDistribution.old-")]
        self.assertEqual(len(olds), 1)
        self.assertNotIn("20260101000000", olds[0])
        self.assertNotIn("20260601000000", olds[0])  # the one just made is the newest

    def test_net_reset_refuses_to_run_unattended(self):
        with mock.patch.object(O, "_stream") as st:
            with self.assertRaises(PermissionError):
                O.repair(Job(), "net", unattended=True)

            class Sub(Job):  # what Autopilot passes: a wrapper with a parent
                parent = object()
            with self.assertRaises(PermissionError):
                O.repair(Sub(), "net")
        st.assert_not_called()

    def test_net_reset_saves_the_settings_first_and_says_where(self):
        def run(args, timeout=0):
            return 0, "netsh dump" if "dump" in args else "Configuration for interface Ethernet"
        with mock.patch.object(O.rsys, "run", side_effect=run), mock.patch.object(O, "ps_json", return_value=[{"alias": "Ethernet"}]), \
                mock.patch.object(O, "_stream", return_value=0) as st:
            r = O.repair(Job(), "net")
        folder = r["backup"]
        self.assertTrue(folder.startswith(os.path.join(self.user, "network")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "restore.netsh")))
        self.assertIn(folder, r["msg"])
        self.assertEqual(st.call_count, 3)

    def test_net_reset_does_not_run_if_the_settings_cannot_be_saved(self):
        with mock.patch.object(O.rsys, "run", return_value=(1, "")), mock.patch.object(O, "_stream") as st:
            r = O.repair(Job(), "net")
        st.assert_not_called()
        self.assertEqual(r["code"], 1)
        self.assertIn("error", r)

    def test_chkdsk_uses_the_system_drive(self):
        with mock.patch.dict(os.environ, {"SystemDrive": "D:"}), mock.patch.object(O, "_stream", return_value=0) as st:
            O.repair(Job(), "chkdsk")
        self.assertEqual(st.call_args[0][1], ["chkdsk", "D:", "/scan"])


class DebloatSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sh_debloat_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(D, "STATE_DIR", self.tmp), mock.patch.object(D, "_admin", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    class Reg:
        def __init__(self):
            self.vals, self.writes = {}, 0

        def get(self, hive, key, name):
            return self.vals.get((hive, key.lower(), name.lower()))

        def key_exists(self, hive, key):
            return True

        def set(self, hive, key, name, typ, data):
            self.writes += 1
            self.vals[(hive, key.lower(), name.lower())] = (typ, data)

        def delete(self, *a):
            pass

        def delete_key_if_empty(self, *a):
            pass

    def test_policies_tweaks_need_admin(self):
        for tid in ("web_search", "copilot", "recall"):
            self.assertTrue(D.needs_admin({t["id"]: t for t in D.TWEAKS}[tid]), tid)
        self.assertFalse(D.needs_admin({t["id"]: t for t in D.TWEAKS}["tb_left"]))

    def test_a_tweak_is_not_applied_if_its_backup_cannot_be_saved(self):
        reg = self.Reg()
        with mock.patch.object(D, "_save_state", side_effect=OSError("read-only")):
            r = D.apply(Job(), {"tweaks": ["tb_left"]}, reg=reg)
        self.assertEqual(reg.writes, 0)
        self.assertEqual(r["tweaks"], [])
        self.assertIn("backup", r["tweakFailed"][0]["error"])

    def test_the_backup_is_on_disk_before_the_first_write(self):
        reg = self.Reg()
        seen = []
        orig = reg.set
        reg.set = lambda *a: seen.append(D._load_json(D._state_path(), {}).get("tweaks", {}).keys()) or orig(*a)
        D.apply(Job(), {"tweaks": ["tb_left"]}, reg=reg)
        self.assertEqual(list(seen[0]), ["tb_left"])

    def test_failed_write_leaves_no_backup_behind(self):
        reg = self.Reg()
        reg.set = mock.Mock(side_effect=OSError("denied"))
        r = D.apply(Job(), {"tweaks": ["tb_left"]}, reg=reg)
        self.assertEqual(r["tweaks"], [])
        self.assertEqual(D._state()["tweaks"], {})

    def test_app_is_not_removed_without_its_record(self):
        row = {"PackageFullName": "Microsoft.BingNews_1_x64__abc", "Name": "Microsoft.BingNews", "PackageFamilyName": "Microsoft.BingNews_abc",
               "InstallLocation": "", "Version": "1"}
        with mock.patch.object(D, "_installed", return_value=[row]), mock.patch.object(D, "_provisioned", return_value=None), \
                mock.patch.object(D, "_save_state", side_effect=OSError("read-only")), mock.patch.object(D, "_ps_ok") as ps_ok:
            r = D.apply(Job(), {"apps": [row["PackageFullName"]]}, reg=self.Reg())
        ps_ok.assert_not_called()
        self.assertEqual(r["removed"], [])
        self.assertEqual(len(r["failed"]), 1)

    def test_failed_removal_drops_its_record(self):
        row = {"PackageFullName": "Microsoft.BingNews_1_x64__abc", "Name": "Microsoft.BingNews", "PackageFamilyName": "Microsoft.BingNews_abc",
               "InstallLocation": "", "Version": "1"}
        with mock.patch.object(D, "_installed", return_value=[row]), mock.patch.object(D, "_provisioned", return_value=None), \
                mock.patch.object(D, "_ps_ok", return_value=(False, "nope")):
            r = D.apply(Job(), {"apps": [row["PackageFullName"]]}, reg=self.Reg())
        self.assertEqual(r["removed"], [])
        self.assertEqual(D._state()["apps"], [])


class PrivacySafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(P, "DATA", self.tmp), mock.patch.object(P, "_admin", lambda: False)):
            p.start()
            self.addCleanup(p.stop)

    def test_no_change_without_a_backup(self):
        class Reg:
            writes = 0

            def read(self, *a):
                return False, None, None

            def write(self, *a):
                Reg.writes += 1

            def delete(self, *a):
                Reg.writes += 1
        with mock.patch.object(P, "REG", Reg()), mock.patch.object(P, "_write_json", side_effect=OSError("read-only")):
            with self.assertRaises(OSError) as cm:
                P.tracking_set(["tips"], False)
        self.assertEqual(Reg.writes, 0)
        self.assertIn("backup", str(cm.exception))

    def test_autostart_change_is_saved_first(self):
        with mock.patch.object(P, "_write_json", side_effect=OSError("read-only")), mock.patch.object(P, "_set_autostart") as sa:
            with self.assertRaises(OSError):
                P.persist_set("run|HKCU|x", False, "x")
        sa.assert_not_called()

    def test_firewall_fix_that_windows_refuses_is_not_reported_done(self):
        rows = [{"n": n, "on": False} for n in P._FW_PROFILES]
        with mock.patch.object(P, "_admin", lambda: True), mock.patch.object(P, "ps_json", return_value=rows), \
                mock.patch.object(P, "ps", return_value="Set-NetFirewallProfile : Access is denied.\r\n"):
            with self.assertRaises(OSError):
                P.fix("firewall")
        self.assertTrue(all(h["undone"] for h in P.history()))     # nothing left to "undo"

    def test_firewall_fix_needs_the_current_state_first(self):
        with mock.patch.object(P, "_admin", lambda: True), mock.patch.object(P, "ps_json", return_value=[]), \
                mock.patch.object(P, "ps") as run:
            with self.assertRaises(OSError):
                P.fix("firewall")
        run.assert_not_called()
        with mock.patch.object(P, "_admin", lambda: True), mock.patch.object(P, "ps_json", return_value=[
                {"n": n, "on": False} for n in P._FW_PROFILES]), mock.patch.object(P, "ps", return_value="ok\r\n"):
            self.assertTrue(P.fix("firewall")["ok"])

    def test_firewall_undo_that_windows_refuses_can_be_tried_again(self):
        rows = [{"n": n, "on": False} for n in P._FW_PROFILES]
        with mock.patch.object(P, "_admin", lambda: True), mock.patch.object(P, "ps_json", return_value=rows), \
                mock.patch.object(P, "ps", return_value="ok\r\n"):
            bid = P.fix("firewall")["backup"]
        with mock.patch.object(P, "_admin", lambda: True), \
                mock.patch.object(P, "ps", return_value="Set-NetFirewallProfile : Access is denied.\r\n"):
            with self.assertRaises(OSError):
                P.undo(bid)
        self.assertFalse(next(h for h in P.history() if h["id"] == bid)["undone"])
        with mock.patch.object(P, "_admin", lambda: True), mock.patch.object(P, "ps", return_value="ok\r\n"):
            self.assertTrue(P.undo(bid)["ok"])
        self.assertTrue(next(h for h in P.history() if h["id"] == bid)["undone"])

    def test_old_programdata_records_are_copied_to_the_user_folder(self):
        legacy = os.path.join(self.tmp, "old")
        new = os.path.join(self.tmp, "new")
        tag = P._user_tag()
        os.makedirs(os.path.join(legacy, "changes", tag))
        with open(os.path.join(legacy, "changes", tag, "20260101-000000.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "20260101-000000", "label": "old", "undone": False, "t": 1}, fh)
        with mock.patch.object(P, "DATA", new), mock.patch.object(P, "_DEFAULT_DATA", new), mock.patch.object(P, "_LEGACY", legacy), \
                mock.patch.object(P, "_migrated", set()):
            self.assertEqual([h["id"] for h in P.history()], ["20260101-000000"])


class BackupsListing(unittest.TestCase):
    def test_debloat_state_is_not_a_backup_and_both_roots_are_merged(self):
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, b, ignore_errors=True)
        bid = "20261119-120000-abcd"
        for root, k in ((a, r"HKLM\x"), (b, r"HKCU\y")):
            os.makedirs(os.path.join(root, "registry", bid))
            with open(os.path.join(root, "registry", bid, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump([{"kind": "value", "k": k}], fh)
        os.makedirs(os.path.join(a, "debloat", "x"))
        with mock.patch.object(B, "OPT_DIR", a), mock.patch.object(B, "USER_OPT_DIR", b):
            out = B.optimizer_backups()
        self.assertEqual([x["id"] for x in out], ["opt-registry|" + bid])
        self.assertEqual(out[0]["count"], 2)
        self.assertEqual(len(out[0]["paths"]), 2)


class RegistryBackupsExist(unittest.TestCase):
    """The registry cleaner is opt-in; a user who already has registry-fix backups keeps it on after upgrading."""

    def _dirs(self):
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, b, ignore_errors=True)
        return a, b

    def test_false_without_registry_backups(self):
        a, b = self._dirs()
        os.makedirs(os.path.join(a, "services"))
        os.makedirs(os.path.join(b, "registry"))  # empty folder: no backup
        with mock.patch.object(O, "DATA", a), mock.patch.object(O, "USER_DATA", b):
            self.assertFalse(O.registry_backups_exist())
        with mock.patch.object(O, "DATA", os.path.join(a, "missing")), mock.patch.object(O, "USER_DATA", os.path.join(b, "nope")):
            self.assertFalse(O.registry_backups_exist())

    def test_true_with_a_backup_in_either_root(self):
        a, b = self._dirs()
        os.makedirs(os.path.join(b, "registry", "20261119-120000-abcdef"))
        with mock.patch.object(O, "DATA", a), mock.patch.object(O, "USER_DATA", b):
            self.assertTrue(O.registry_backups_exist())
        with mock.patch.object(O, "DATA", b), mock.patch.object(O, "USER_DATA", a):
            self.assertTrue(O.registry_backups_exist())
        self.assertIn("registry_backups_exist", O.EXPORTS)


class MonitorProcesses(unittest.TestCase):
    def setUp(self):
        M._pdh, M._pdh_next = None, 0.0
        self.addCleanup(lambda: setattr(M, "_pdh", None))
        self.addCleanup(lambda: setattr(M, "_pdh_next", 0.0))
        for p in (mock.patch.object(M, "_proc_icon", return_value=None), mock.patch.object(M, "_file_desc", return_value=None),
                  mock.patch.object(M, "_find_exe", return_value=""),
                  mock.patch.object(M.psutil, "net_connections", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    class Dead:
        c = {}

        def __init__(self, paths):
            pass

        def healthy(self):
            return False

        def close(self):
            pass

    def test_falls_back_to_psutil_and_retries_later(self):
        with mock.patch.object(M, "_PdhArray", self.Dead):
            r = M.processes()
        self.assertNotIn("error", r)
        self.assertGreater(len(r["list"]), 3)
        self.assertFalse(r["gpuPerProcess"])
        self.assertIsNone(M._pdh)
        self.assertGreater(M._pdh_next, 0)
        made = []

        class Fine(self.Dead):
            c = {"pid": 1}

            def __init__(s, paths):
                made.append(1)

            def healthy(s):
                return True

            def collect(s):
                return True

            def array(s, key):
                return [("python", os.getpid())] if key == "pid" else [("python", 1.0)]
        with mock.patch.object(M, "_PdhArray", Fine):
            M.processes()
            self.assertEqual(made, [])  # too soon: still the fallback
            M._pdh_next = 0.0           # a minute later
            r = M.processes()
        self.assertEqual(made, [1])
        self.assertEqual(r["total"], 1)

    def test_empty_pid_counter_also_falls_back(self):
        class NoPid(self.Dead):
            c = {"pid": 1}

            def healthy(s):
                return True

            def collect(s):
                return True

            def array(s, key):
                return []
        with mock.patch.object(M, "_PdhArray", NoPid):
            r = M.processes()
        self.assertGreater(len(r["list"]), 3)
        self.assertGreater(M._pdh_next, 0)


class DnsUndo(unittest.TestCase):
    def test_previous_dns_is_saved_before_changing_and_can_be_restored(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cmds = []

        def run(c, **k):
            cmds.append(c)
            return mock.Mock(returncode=0, stdout=b"")
        with mock.patch.object(M, "DNS_BACKUPS", tmp), mock.patch.object(M, "_admin", return_value=True), \
                mock.patch.object(M, "_current_dns", return_value=(False, ["192.168.1.2", "192.168.1.3"])), \
                mock.patch.object(M.subprocess, "run", side_effect=run):
            r = M.set_dns("Ethernet", "auto")
            self.assertTrue(r["ok"])
            self.assertTrue(os.path.isfile(os.path.join(tmp, r["backup"] + ".json")))
            cmds.clear()
            self.assertTrue(M.dns_undo(r["backup"])["ok"])
        self.assertIn("address=192.168.1.2", " ".join(cmds[0]))
        self.assertIn("index=2", " ".join(cmds[1]))

    def test_nothing_changes_if_the_previous_dns_cannot_be_saved(self):
        with mock.patch.object(M, "_admin", return_value=True), mock.patch.object(M, "_current_dns", return_value=(False, ["1.2.3.4"])), \
                mock.patch.object(M, "_save_dns", side_effect=OSError("denied")), mock.patch.object(M.subprocess, "run") as run:
            r = M.set_dns("Ethernet", "auto")
        run.assert_not_called()
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()

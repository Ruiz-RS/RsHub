"""Always open as administrator: only an all-users install keeps the scheduled task that opens RsHub elevated
without the Windows prompt. A per-user install (%LOCALAPPDATA%\\Programs), a portable or a source copy lives in a folder
the user's own programs can write to, so a task would run whatever exe is there elevated: those ask Windows at each
start, and an elevated RsHub removes a task an earlier version left for them. Nothing here creates, runs or deletes
a real task: schtasks and the registry are replaced by mocks."""
import os
import re
import subprocess
import unittest
from unittest import mock

from rshub import config, elevation
from rshub.api import Api

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PF = r"C:\Program Files"
LOCAL = r"C:\Users\someone\AppData\Local\Programs"


def known(guid):
    return {elevation.PROGRAM_FILES[0]: PF, elevation.PROGRAM_FILES[1]: PF,
            elevation.PROGRAM_FILES[2]: r"C:\Program Files (x86)"}.get(guid)


class Scope(unittest.TestCase):
    def frozen(self, exe, portable=False, registered=None):
        ps = [mock.patch.object(config, "FROZEN", True), mock.patch.object(config, "PORTABLE", portable),
              mock.patch.object(elevation.sys, "executable", exe),
              mock.patch.object(elevation, "_known_folder", known),
              mock.patch.object(elevation, "_registered_for_all_users", lambda d: registered is not None and
                                os.path.normcase(d) == os.path.normcase(registered))]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def test_program_files_uses_the_task(self):
        self.frozen(PF + r"\RsHub\RsHub.exe")
        self.assertTrue(elevation.uses_admin_task())

    def test_a_per_user_install_does_not(self):
        self.frozen(LOCAL + r"\RsHub\RsHub.exe")
        self.assertFalse(elevation.uses_admin_task())

    def test_a_custom_folder_registered_for_all_users_does(self):
        self.frozen(r"D:\Apps\RsHub\RsHub.exe", registered=r"D:\Apps\RsHub")
        self.assertTrue(elevation.uses_admin_task())
        self.assertFalse(elevation.per_machine_folder(r"D:\Apps\Other"))

    def test_the_portable_and_the_source_copies_do_not(self):
        self.frozen(PF + r"\RsHub\RsHub.exe", portable=True)
        self.assertFalse(elevation.uses_admin_task())
        with mock.patch.object(config, "PORTABLE", False), mock.patch.object(config, "FROZEN", False):
            self.assertFalse(elevation.uses_admin_task())

    def test_the_program_files_variable_cannot_be_spoofed(self):
        """%ProgramFiles% can be changed by the user; Windows' known folder is what counts."""
        self.frozen(LOCAL + r"\RsHub\RsHub.exe")
        with mock.patch.dict(os.environ, {"ProgramFiles": LOCAL, "ProgramW6432": LOCAL}):
            self.assertFalse(elevation.uses_admin_task())

    def test_a_per_user_install_never_creates_the_task(self):
        self.frozen(LOCAL + r"\RsHub\RsHub.exe")
        with mock.patch("rshub.schtask.create") as create:
            self.assertFalse(elevation.ensure_admin_task())
        create.assert_not_called()

    def test_an_all_users_install_still_creates_it(self):
        self.frozen(PF + r"\RsHub\RsHub.exe")
        with mock.patch("rshub.schtask.create", return_value=True) as create, mock.patch("subprocess.run"):
            self.assertTrue(elevation.ensure_admin_task())
        create.assert_called_once()

    def test_a_per_user_start_asks_windows_and_never_runs_an_old_task(self):
        self.frozen(LOCAL + r"\RsHub\RsHub.exe")
        with mock.patch.object(elevation, "wants_elevation", return_value=True), \
                mock.patch.object(elevation, "_hand_over_splash", return_value=None), \
                mock.patch.object(elevation, "task_opens_this_copy", return_value=True), \
                mock.patch("subprocess.run") as run, \
                mock.patch.object(elevation, "elevated_copy_started", return_value=True), \
                mock.patch.object(elevation.ctypes.windll.shell32, "ShellExecuteW", return_value=42) as uac:
            self.assertTrue(elevation.elevate_on_start())
        self.assertFalse(any("/run" in c[0][0] for c in run.call_args_list if c[0]))
        uac.assert_called_once()
        self.assertEqual(uac.call_args[0][1], "runas")


    def test_a_task_scheduler_that_does_not_answer_falls_back_to_the_windows_prompt(self):
        """schtasks /run waited 15 s and then raised TimeoutExpired, which nothing caught: the start ended with a
        traceback and no window. Now it is a failed run and Windows is asked the usual way."""
        self.frozen(PF + r"\RsHub\RsHub.exe")

        def run(cmd, **kw):
            if "/run" in cmd:
                raise subprocess.TimeoutExpired(cmd, 15)
            raise AssertionError(cmd)
        with mock.patch.object(elevation, "wants_elevation", return_value=True), \
                mock.patch.object(elevation, "_hand_over_splash", return_value=None), \
                mock.patch.object(elevation, "task_opens_this_copy", return_value=True), \
                mock.patch("subprocess.run", side_effect=run), \
                mock.patch.object(elevation, "elevated_copy_started", return_value=True), \
                mock.patch.object(elevation.ctypes.windll.shell32, "ShellExecuteW", return_value=42) as uac:
            self.assertTrue(elevation.elevate_on_start())
        uac.assert_called_once()
        self.assertEqual(uac.call_args[0][1], "runas")


class StaleTask(unittest.TestCase):
    """An elevated copy removes "open as administrator" tasks that start a program a user-level process could replace."""

    def query(self, command, args=""):
        xml = (f'<?xml version="1.0" encoding="UTF-16"?><Task><Actions><Exec><Command>{command}</Command>'
               f'<Arguments>{args}</Arguments></Exec></Actions></Task>')
        return mock.Mock(returncode=0, stdout=b"\xff\xfe" + xml.encode("utf-16-le"))

    def run_with(self, tasks):
        """tasks: name -> (command, args) or missing. Returns the names schtasks was asked to delete."""
        deleted = []

        def run(cmd, **kw):
            name = cmd[cmd.index("/tn") + 1]
            if "/query" in cmd:
                return self.query(*tasks[name]) if name in tasks else mock.Mock(returncode=1, stdout=b"")
            if "/delete" in cmd:
                deleted.append(name)
                return mock.Mock(returncode=0)
            raise AssertionError(cmd)
        with mock.patch("subprocess.run", side_effect=run), mock.patch.object(elevation, "_known_folder", known), \
                mock.patch.object(elevation, "_registered_for_all_users", return_value=False):
            gone = elevation.remove_unsafe_admin_task()
        self.assertEqual(gone, deleted)
        return deleted

    def test_a_task_for_a_per_user_install_is_removed(self):
        self.assertEqual(self.run_with({elevation.ADMIN_TASK: (LOCAL + r"\RsHub\RsHub.exe", "--from-task")}),
                         [elevation.ADMIN_TASK])

    def test_a_task_for_a_source_copy_is_removed(self):
        """python + run.py in the user's documents: the python may be in Program Files, the script is not."""
        self.assertEqual(self.run_with({elevation.ADMIN_TASK: (PF + r"\Python313\pythonw.exe",
                                                               r'"C:\Users\someone\Documents\RsHub\run.py" --from-task')}),
                         [elevation.ADMIN_TASK])

    def test_an_old_named_task_is_removed_too(self):
        old = elevation.LEGACY_ADMIN_TASKS[0]
        self.assertEqual(self.run_with({old: (r"D:\Portable\RsHub\RsHub.exe", "--from-task")}), [old])

    def test_the_all_users_install_keeps_its_task(self):
        self.assertEqual(self.run_with({elevation.ADMIN_TASK: (PF + r"\RsHub\RsHub.exe", "--from-task")}), [])

    def test_the_xml_escapes_are_undone_before_judging(self):
        self.assertEqual(self.run_with({elevation.ADMIN_TASK: ("&quot;" + PF + r"\Tom &amp; Jerry\RsHub.exe&quot;", "")}), [])

    def test_no_task_nothing_to_do(self):
        self.assertEqual(self.run_with({}), [])

    def test_an_elevated_per_user_copy_cleans_up_at_start(self):
        with mock.patch.object(elevation, "uses_admin_task", return_value=False), \
                mock.patch.object(elevation, "remove_unsafe_admin_task") as rm, \
                mock.patch.object(elevation, "ensure_admin_task") as make:
            elevation.sync_admin_task()
        rm.assert_called_once()
        make.assert_not_called()
        with mock.patch.object(elevation, "uses_admin_task", return_value=True), \
                mock.patch.object(elevation.settings, "read", return_value={"alwaysAdmin": True}), \
                mock.patch.object(elevation, "admin_task_current", return_value=False), \
                mock.patch.object(elevation, "remove_unsafe_admin_task") as rm, \
                mock.patch.object(elevation, "ensure_admin_task") as make:
            elevation.sync_admin_task()
        rm.assert_not_called()
        make.assert_called_once()

    def test_the_setting_in_a_per_user_install(self):
        """Turned on while elevated: no task is made (and it is not an error); an old unsafe one goes."""
        api = Api()
        with mock.patch.object(Api, "settings_set", return_value=True), \
                mock.patch.object(elevation, "uses_admin_task", return_value=False), \
                mock.patch("rshub.api.settings_api.config.PORTABLE", False), \
                mock.patch("rshub.api.settings_api.config.ISOLATED", False), \
                mock.patch("rshub.api.settings_api.config.is_admin", return_value=True), \
                mock.patch.object(elevation, "ensure_admin_task") as make, \
                mock.patch.object(elevation, "delete_admin_task") as drop, \
                mock.patch.object(elevation, "remove_unsafe_admin_task") as rm:
            self.assertTrue(api.set_always_admin(True))
            self.assertTrue(api.set_always_admin(False))
        make.assert_not_called()
        drop.assert_not_called()
        self.assertEqual(rm.call_count, 2)

    def test_the_interface_is_told(self):
        with mock.patch.object(elevation, "uses_admin_task", return_value=False):
            self.assertIs(Api().info()["adminTask"], False)


class Installer(unittest.TestCase):
    """The elevated installer of an all-users install never runs a program from the user's %TEMP%."""

    def setUp(self):
        with open(os.path.join(ROOT, "packaging", "RsHub.iss"), encoding="utf-8-sig") as fh:
            self.iss = fh.read()

    def test_the_webview2_bootstrapper_runs_from_the_install_folder_when_elevated(self):
        run = re.search(r"^\[Run\]\s*$(.*?)^\[", self.iss, re.S | re.M).group(1)
        lines = [ln for ln in run.splitlines() if "WebView" in ln and not ln.lstrip().startswith(";")]
        self.assertEqual(len(lines), 1)
        self.assertIn('Filename: "{code:WebView2Setup}"', lines[0])
        self.assertIn("WebView2SetupTrusted", lines[0])
        fn = re.search(r"function WebView2Setup\(Param: String\): String;(.*?)\nend;", self.iss, re.S).group(1)
        self.assertRegex(fn, r"if IsAdminInstallMode then\s+Result := ExpandConstant\('\{app\}\\update\\")
        trusted = re.search(r"function WebView2SetupTrusted: Boolean;(.*?)\nend;", self.iss, re.S).group(1)
        self.assertIn("FileCopy(", trusted)  # checked on the copy it then runs
        self.assertIn("F := WebView2Setup('')", trusted)

    def test_code_comments_hold_no_braces(self):
        """A { comment } in [Code] ends at the first closing brace: "{tmp}" inside one broke the compile."""
        code = self.iss[re.search(r"^\[Code\]$", self.iss, re.M).start():]
        code = re.sub(r"'[^'\n]*'", "''", code)  # strings may hold braces
        for m in re.finditer(r"\{([^}]*)\}", code):
            self.assertNotIn("{", m.group(1), m.group(0)[:80])

    def test_powershell_only_loads_its_own_modules(self):
        trusted = re.search(r"function WebView2SetupTrusted: Boolean;(.*?)\nend;", self.iss, re.S).group(1)
        self.assertIn("$env:PSModulePath = $PSHOME", trusted)
        self.assertIn("Microsoft.PowerShell.Security\\Get-AuthenticodeSignature", trusted)

    def test_every_program_the_elevated_installer_runs_is_in_a_protected_place(self):
        """Exec / [Run] / [UninstallRun] start {app}\\RsHub.exe, a {sys} program or the copy above; {tmp} only
        through WebView2Setup (and only when not elevated)."""
        for m in re.finditer(r"\bExec\(([^,]+),", self.iss):
            self.assertRegex(m.group(1).strip(), r"^(Exe|ExpandConstant\('\{sys\}\\)")
        for m in re.finditer(r'^Filename: "([^"]+)"', self.iss, re.M):
            self.assertRegex(m.group(1), r"^(\{app\}\\|\{code:WebView2Setup\}$|\{#AppUrl\})")


if __name__ == "__main__":
    unittest.main()

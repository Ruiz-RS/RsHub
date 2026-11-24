"""RsHub's scheduled tasks must start on battery, never be ended after 72 hours and run at normal priority
(Windows' defaults for `schtasks /create /tr` did all three wrong: with "Always open as administrator" RsHub
didn't open at all on a laptop running on battery)."""
import ctypes
import os
import subprocess
import unittest
import uuid
from unittest import mock
from xml.dom import minidom

from rshub import schtask
from rshub.engine import selfupdate as su

OLD_TASK = """<Task><Settings><DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
<StopIfGoingOnBatteries>true</StopIfGoingOnBatteries><ExecutionTimeLimit>PT72H</ExecutionTimeLimit>
<Priority>7</Priority></Settings></Task>"""


def _text(dom, tag):
    return dom.getElementsByTagName(tag)[0].firstChild.nodeValue


class Xml(unittest.TestCase):
    def test_runs_on_battery_without_time_limit_at_normal_priority(self):
        x = schtask.task_xml(r"C:\Program Files\RsHub\RsHub.exe", "--from-task", "Opens & more")
        dom = minidom.parseString(x.split("?>", 1)[1])
        self.assertEqual(_text(dom, "DisallowStartIfOnBatteries"), "false")
        self.assertEqual(_text(dom, "StopIfGoingOnBatteries"), "false")
        self.assertEqual(_text(dom, "ExecutionTimeLimit"), "PT0S")
        self.assertEqual(_text(dom, "Priority"), "4")
        self.assertEqual(_text(dom, "RunLevel"), "HighestAvailable")
        self.assertEqual(_text(dom, "Command"), r"C:\Program Files\RsHub\RsHub.exe")
        self.assertEqual(_text(dom, "Description"), "Opens & more")  # escaped in the XML, intact once read

    def test_old_tasks_are_noticed(self):
        self.assertTrue(schtask.outdated(OLD_TASK))
        self.assertFalse(schtask.outdated(schtask.task_xml("x.exe")))
        self.assertFalse(schtask.outdated(""))  # no task: nothing to repair


class UpdaterFallsBack(unittest.TestCase):
    def test_a_task_that_never_starts_is_ended_and_no_prompt_in_the_background(self):
        ran = []

        def fake_run(args, **kw):
            ran.append(args[:2])
            return subprocess.CompletedProcess(args, 0, b"", b"")
        with mock.patch.object(su, "mode", lambda: "task"), mock.patch.object(su, "_clear_cancel", lambda: None), \
                mock.patch.object(su, "_updater_answered", lambda at, seconds=8: False), \
                mock.patch.object(su.subprocess, "run", fake_run), mock.patch.object(schtask.subprocess, "run", fake_run), \
                mock.patch.object(su.ctypes.windll.shell32, "ShellExecuteW") as uac:
            r = su.start(allow_uac=False)
        self.assertFalse(r["ok"])
        self.assertIn(["schtasks", "/end"], ran)  # the queued run is dropped, so it can't start later by surprise
        uac.assert_not_called()

    def test_a_task_that_starts_is_used(self):
        with mock.patch.object(su, "mode", lambda: "task"), mock.patch.object(su, "_clear_cancel", lambda: None), \
                mock.patch.object(su, "_updater_answered", lambda at, seconds=8: True), \
                mock.patch.object(su.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)):
            r = su.start(allow_uac=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "task")


@unittest.skipUnless(os.environ.get("RSHUB_SYSTEM_TESTS") == "1" and ctypes.windll.shell32.IsUserAnAdmin(),
                     "creates a real scheduled task: only on request (RSHUB_SYSTEM_TESTS=1, as administrator); "
                     "the suite never changes the system")
class RealTask(unittest.TestCase):
    def test_windows_accepts_the_task_and_keeps_the_settings(self):
        name = rf"RsHubTest\t{uuid.uuid4().hex[:8]}"
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
        try:
            self.assertTrue(schtask.create(name, exe, "/c exit 0", "test task"))
            got = schtask.query_xml(name)
            self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", got)
            self.assertIn("<Priority>4</Priority>", got)
            self.assertFalse(schtask.outdated(got))
        finally:
            subprocess.run(["schtasks", "/delete", "/tn", name, "/f"], capture_output=True)
            subprocess.run(["schtasks", "/delete", "/tn", "RsHubTest", "/f"], capture_output=True)


if __name__ == "__main__":
    unittest.main()

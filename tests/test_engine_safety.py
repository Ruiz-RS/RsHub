"""Safety nets in the engines: stress test thermal stop, cancellable/timed-out processes, friendly tool errors,
partial driver backups, installs cancelled halfway and winget failures that must not read as "up to date"."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from rshub.engine import benchmark as B
from rshub.engine import drivers as D
from rshub.engine import ps
from rshub.engine import updater as U
from rshub.engine.jobs import Cancelled, Job


class QuietJob(Job):
    def __init__(self):
        super().__init__("test")

    def _send(self, kind):
        pass


class StressSafety(unittest.TestCase):
    def test_stops_at_the_temperature_limit_without_the_ui(self):
        with mock.patch.object(B, "_read_cpu_temp", return_value=B.STRESS_CPU_LIMIT + 1):
            r = B.stress_cpu(QuietJob(), seconds=60, threads=1)
        self.assertTrue(r["stopped"])
        self.assertEqual(r["reason"]["part"], "cpu")
        self.assertLess(r["elapsed"], 5)

    def test_hard_maximum_duration(self):
        with mock.patch.object(B, "_read_cpu_temp", return_value=50), mock.patch.object(B, "STRESS_MAX_SECONDS", 5):
            r = B.stress_cpu(QuietJob(), seconds=99999, threads=1)
        self.assertLess(r["elapsed"], 8)
        self.assertIsNone(r["reason"])

    def test_no_sensor_stops_only_when_temperature_is_required(self):
        with mock.patch.object(B, "_read_cpu_temp", return_value=None), mock.patch.object(B, "_NO_TEMP_GRACE", 0.4):
            r = B.stress_cpu(QuietJob(), seconds=60, threads=1, require_temp=True)
        self.assertEqual(r["reason"], {"part": "notemp"})
        self.assertLess(r["elapsed"], 5)


class RunProcess(unittest.TestCase):
    SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]

    def test_cancel_is_noticed_while_the_program_is_silent(self):
        job = QuietJob()
        threading.Timer(0.8, lambda: setattr(job, "cancelled", True)).start()
        t0 = time.monotonic()
        ps.run(self.SLEEP, job=job)
        self.assertLess(time.monotonic() - t0, 8)

    def test_timeout_applies_to_a_silent_program(self):
        t0 = time.monotonic()
        ps.run(self.SLEEP, timeout=1)
        self.assertLess(time.monotonic() - t0, 8)
        with self.assertRaises(subprocess.TimeoutExpired):
            ps.run(self.SLEEP, timeout=1, raise_on_timeout=True)

    def test_output_is_returned(self):
        lines = []
        code, out = ps.run([sys.executable, "-c", "print('hola'); print('adios')"], on_line=lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["hola", "adios"])

    def test_missing_program_gives_a_short_error(self):
        with self.assertRaises(ps.ToolError) as c:
            ps.run(["definitely-not-a-real-tool.exe", "/x"])
        self.assertIn("definitely-not-a-real-tool", str(c.exception))
        self.assertNotIn("WinError", str(c.exception))

    def test_powershell_timeout_and_missing(self):
        with mock.patch.object(ps.subprocess, "Popen", side_effect=FileNotFoundError("x")):
            with self.assertRaises(ps.ToolError) as c:
                ps.ps("Get-Date")
        self.assertIn("PowerShell", str(c.exception))
        p = mock.Mock(pid=1)
        p.communicate.side_effect = [subprocess.TimeoutExpired("x", 1), (b"", b"")]
        with mock.patch.object(ps.subprocess, "Popen", return_value=p), mock.patch.object(ps, "kill_tree") as k:
            with self.assertRaises(ps.ToolError) as c:
                ps.ps("Start-Sleep 99 # a long script text", timeout=1)
        self.assertNotIn("Start-Sleep", str(c.exception))
        k.assert_called_once()


class DriverBackupsAndInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_cancelled_backup_leaves_nothing_listed(self):
        job = QuietJob()

        def fake_run(cmd, on_line=None, **kw):
            with open(os.path.join(cmd[-1], "oem1.inf"), "w") as f:
                f.write("x")
            job.cancelled = True
            return 1, ""
        with mock.patch.object(D, "default_backup_dir", return_value=self.tmp), \
                mock.patch.object(D, "KNOWN_BACKUPS", os.path.join(self.tmp, "known.json")), \
                mock.patch.object(D, "_need_admin", return_value=None), \
                mock.patch.object(D, "_third_party_count", return_value=1), \
                mock.patch.object(D, "run", side_effect=fake_run):
            with self.assertRaises(Cancelled):
                D.export(job)
            self.assertEqual([d for d in os.listdir(self.tmp) if os.path.isdir(os.path.join(self.tmp, d))], [])
            # an interrupted one (app killed) carries the marker and is never listed
            part = os.path.join(self.tmp, "2020-01-01 0000")
            os.makedirs(part)
            open(os.path.join(part, "oem1.inf"), "w").close()
            open(os.path.join(part, D.PARTIAL), "w").close()
            self.assertEqual(D.backups(), [])

    def test_a_cancelled_backup_never_removes_an_earlier_one_from_the_same_minute(self):
        job = QuietJob()
        first = os.path.join(self.tmp, "2026-11-22 1530")
        os.makedirs(first)
        with open(os.path.join(first, "oem1.inf"), "w") as f:
            f.write("complete backup")

        def fake_run(cmd, on_line=None, **kw):
            self.assertNotEqual(os.path.normcase(cmd[-1]), os.path.normcase(first))
            job.cancelled = True
            return 1, ""
        with mock.patch.object(D, "default_backup_dir", return_value=self.tmp), \
                mock.patch.object(D, "KNOWN_BACKUPS", os.path.join(self.tmp, "known.json")), \
                mock.patch.object(D, "_need_admin", return_value=None), \
                mock.patch.object(D, "_third_party_count", return_value=1), \
                mock.patch.object(D.time, "strftime", return_value="2026-11-22 1530"), \
                mock.patch.object(D, "run", side_effect=fake_run):
            with self.assertRaises(Cancelled):
                D.export(job)
        self.assertTrue(os.path.isfile(os.path.join(first, "oem1.inf")))

    def test_cancelled_install_keeps_what_was_installed(self):
        job = QuietJob()
        a, b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        last = os.path.join(self.tmp, "last.json")
        with open(last, "w", encoding="utf-8") as f:
            json.dump({"at": 1, "updates": [{"id": a}, {"id": b}]}, f)

        def fake_run(cmd, on_line=None, **kw):
            on_line(json.dumps({"step": "download", "id": a}))
            on_line(json.dumps({"step": "done", "id": a, "ok": True, "code": 2}))
            on_line(json.dumps({"step": "download", "id": b}))
            job.cancelled = True
            return 1, ""
        with mock.patch.object(D, "LAST", last), mock.patch.object(D, "_need_admin", return_value=None), \
                mock.patch.object(D, "run", side_effect=fake_run):
            with self.assertRaises(Cancelled):
                D.install(job, [a, b], restore_point=False)
        with open(last, encoding="utf-8") as f:
            self.assertEqual([u["id"] for u in json.load(f)["updates"]], [b])


UP_TABLE = ("Name  Id        Version  Available  Source\n"
            "-----------------------------------------\n"
            "App   Foo.Bar   1.0      2.0        winget\n")


class WingetFailures(unittest.TestCase):
    def test_check_failed(self):
        self.assertTrue(U.check_failed(0x8A150002, "Failed when opening source(s)", []))
        self.assertTrue(U.check_failed(0x8A150006, "", []))
        self.assertTrue(U.check_failed(0, "Unable to connect. Check your internet connection", []))
        self.assertFalse(U.check_failed(0, "No installed package found matching input criteria.", []))
        self.assertFalse(U.check_failed(0, UP_TABLE, U.parse_table(UP_TABLE)))
        self.assertFalse(U.check_failed(0x8A15002B, "", []))

    def test_offline_scan_is_not_reported_as_up_to_date(self):
        job = QuietJob()
        with mock.patch.object(U, "_exe", return_value="winget"), \
                mock.patch.object(U, "_winget_ex", return_value=(0x8A150002, "Failed when opening source(s)")), \
                mock.patch.object(U, "_winget", return_value=""), mock.patch.object(U, "_programs", return_value=[]):
            r = U.scan_apps(job)
        self.assertEqual(r.get("failed"), "offline")
        self.assertEqual(r["items"], [])

    def test_winget_timeout_is_friendly(self):
        with mock.patch.object(U, "_exe", return_value="winget"), \
                mock.patch.object(U, "_run", side_effect=subprocess.TimeoutExpired("winget", 1)):
            with self.assertRaises(ps.ToolError) as c:
                U._winget_ex(["list"], 1)
        self.assertIn("too long", str(c.exception))


if __name__ == "__main__":
    unittest.main()

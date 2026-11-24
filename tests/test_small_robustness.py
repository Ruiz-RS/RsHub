"""Small error paths: a stuck Task Scheduler, the driver rollback's error code, winget's progress numbers and a
settings file that is not an object. Nothing real is created, installed or changed."""
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from rshub import i18n, schtask
from rshub.engine import drivers as D
from rshub.engine import hangar as H
from rshub.engine import updater as U


class StuckTaskScheduler(unittest.TestCase):
    def test_create_gives_up_instead_of_waiting_forever(self):
        seen = {}

        def run(cmd, **kw):
            seen["timeout"] = kw.get("timeout")
            seen["xml"] = cmd[cmd.index("/xml") + 1]
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        with mock.patch.object(schtask.subprocess, "run", side_effect=run):
            self.assertFalse(schtask.create(r"RsHubTest\never", "C:\\x.exe"))
        self.assertTrue(seen["timeout"])
        self.assertFalse(os.path.exists(seen["xml"]))  # the temp XML is still removed


    def test_the_task_is_for_the_tokens_account_not_the_environments(self):
        with mock.patch.dict(os.environ, {"USERDOMAIN": "EVIL", "USERNAME": "someone"}):
            user = schtask._user()
        self.assertNotEqual(user, "EVIL" + chr(92) + "someone")
        self.assertIn(chr(92), user)


class RollbackError(unittest.TestCase):
    def test_the_error_is_the_one_windows_gave_for_the_call(self):
        fn = mock.Mock(return_value=0)
        dll = types.SimpleNamespace(UpdateDriverForPlugAndPlayDevicesW=fn)
        with mock.patch.object(D, "_need_admin", return_value=None), mock.patch.object(D.os.path, "isfile", return_value=True), \
                mock.patch.object(D, "_restore_point", return_value={"ok": True}), \
                mock.patch.object(D.ctypes, "WinDLL", return_value=dll) as windll, \
                mock.patch.object(D.ctypes, "get_last_error", return_value=0xE000020B):
            r = D.rollback(types.SimpleNamespace(progress=lambda *a, **k: None), "PCI\\X", "oem12.inf", "PCI\\VEN_1")
        self.assertFalse(r["ok"])
        self.assertIn("0xE000020B", r["msg"])
        self.assertEqual(windll.call_args, mock.call("newdev", use_last_error=True))


class WingetProgressNumbers(unittest.TestCase):
    def test_sizes_in_any_format_and_never_an_error(self):
        self.assertEqual(U.download_sizes("  1.50 MB / 3.00 MB"), (1.5, 3.0))
        self.assertEqual(U.download_sizes("1,5 MB / 3 MB"), (1.5, 3.0))
        self.assertEqual(U.download_sizes("1.234,5 KB / 2.048,0 KB"), U.download_sizes("1,234.5 KB / 2,048.0 KB"))
        self.assertEqual(U.download_sizes("512 KB / 1 GB"), (0.5, 1024.0))
        for odd in (". MB / 3 MB", "1.2.3 MB / 4 MB", "", None, "Downloading https://x/y.exe"):
            self.assertIsNone(U.download_sizes(odd), odd)

    def test_an_odd_progress_line_does_not_stop_reading_winget(self):
        lines = ["Downloading", "  1.2.3 MB / 4 MB", "  2.0 MB / 4.0 MB", "Successfully installed"]

        def stream(cmd, on_text=None, job=None, timeout=None, **kw):
            for ln in lines:
                on_text(ln)
            return 0, "\n".join(lines)
        reports = []
        job = types.SimpleNamespace(cancelled=False, log=lambda s: None)
        with mock.patch.object(U, "_stream", side_effect=stream), mock.patch.object(U, "_exe", return_value="winget"):
            r = U._winget_upgrade(job, {"wid": "A.B"}, lambda *a, **k: reports.append((a, k)))
        self.assertEqual(r[0], "done")
        self.assertIn((("down", 0.5), {"mb": 4.0}), reports)
        with mock.patch.object(H, "_runner", return_value=stream), mock.patch.object(H.U, "_exe", return_value="winget"):
            st, _msg, extra = H._install_one(job, {"id": "A.B"}, {}, lambda *a, **k: None)
        self.assertEqual((st, extra["mb"]), ("done", 4.0))


class SettingsThatAreNotAnObject(unittest.TestCase):
    def test_the_language_still_resolves(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "settings.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(["not", "an", "object"], fh)
        env = {k: v for k, v in os.environ.items() if k != "RSHUB_LANG"}
        with mock.patch.object(i18n, "_lang", None), mock.patch.object(i18n, "_dict", {}), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("rshub.paths.settings_file", return_value=p):
            self.assertIn(i18n.language(), i18n.CODES)


if __name__ == "__main__":
    unittest.main()

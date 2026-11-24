"""Watched install, startup, cleaner, Job Object, shortcuts and settings."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from rshub.engine.removal import cleaner, monitor, settings, shortcuts, startup, uninstaller


class JobObject(unittest.TestCase):
    def test_waits_for_detached_children(self):
        cmd = 'cmd /c start "" /b cmd /c "ping -n 3 127.0.0.1 >nul"'
        t = time.time()
        r = uninstaller.run_tracked(cmd)
        self.assertGreaterEqual(time.time() - t, 1.5)
        self.assertGreaterEqual(r["processes"], 2)
        self.assertFalse(r["cancelled"])

    def test_cancel(self):
        import threading
        ev = threading.Event()
        threading.Timer(0.5, ev.set).start()
        r = uninstaller.run_tracked('cmd /c ping -n 10 127.0.0.1 >nul', cancel=ev)
        self.assertTrue(r["cancelled"])


class Shortcuts(unittest.TestCase):
    def test_reads_real_lnk(self):
        d = tempfile.mkdtemp(prefix="mrl_")
        try:
            lnk = os.path.join(d, "t.lnk")
            target = os.path.join(os.environ["WINDIR"], "notepad.exe")
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
                            f"$s.TargetPath='{target}';$s.Arguments='--x';$s.Save()"], check=True, capture_output=True)
            info = shortcuts.read_lnk(lnk)
            self.assertEqual(info["target"].lower(), target.lower())
            self.assertEqual(info["args"], "--x")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_garbage_does_not_crash(self):
        d = tempfile.mkdtemp(prefix="mrl_")
        try:
            f = os.path.join(d, "bad.lnk")
            with open(f, "wb") as fh:
                fh.write(b"\x4c\x00\x00\x00" + os.urandom(200))
            shortcuts.read_lnk(f)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Monitor(unittest.TestCase):
    def test_topmost(self):
        self.assertEqual(monitor._topmost({r"c:\a", r"c:\a\b", r"c:\ab", r"c:\c\d"}), [r"c:\a", r"c:\ab", r"c:\c\d"])

    def test_snapshot_shape(self):
        s = monitor.snapshot()
        self.assertGreater(len(s["files"]), 100)
        self.assertGreater(len(s["keys"]), 100)


class Startup(unittest.TestCase):
    def test_approved_bytes(self):
        written = {}
        fake_key = mock.MagicMock()
        with mock.patch("winreg.CreateKeyEx", return_value=fake_key), \
                mock.patch("winreg.SetValueEx", side_effect=lambda k, n, r, t, d: written.__setitem__(n, d)):
            startup._set_approved("HKCU", "Run", "X", False)
            self.assertEqual(written["X"][:4], b"\x03\x00\x00\x00")
            self.assertEqual(len(written["X"]), 12)
            startup._set_approved("HKCU", "Run", "X", True)
            self.assertEqual(written["X"], b"\x02" + b"\x00" * 11)


class Cleaner(unittest.TestCase):
    def test_only_contents_and_respects_grace(self):
        d = tempfile.mkdtemp(prefix="mrc_")
        try:
            old, new = os.path.join(d, "sub", "old.tmp"), os.path.join(d, "new.tmp")
            os.makedirs(os.path.dirname(old))
            for f in (old, new):
                with open(f, "w") as fh:
                    fh.write("x" * 100)
            past = time.time() - 10 * 86400   # older than the 7-day temp grace
            os.utime(old, (past, past))
            os.utime(os.path.dirname(old), (past, past))
            cats = {"t": {"id": "t", "label": "t", "group": "g", "globs": [d], "grace": True, "temp": True,
                          "default": True, "desc": ""}}
            with mock.patch.object(cleaner, "categories", return_value=cats):
                r = cleaner.clean(["t"], in_use={})
            self.assertEqual(r["deleted"], 1)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))   # recent: kept
            self.assertTrue(os.path.isdir(d))      # the root is never deleted
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_parallel_walk_matches_simple_walk_and_skips_links(self):
        d = tempfile.mkdtemp(prefix="mrw_")
        outside = tempfile.mkdtemp(prefix="mrw_out_")
        try:
            past = time.time() - 7200
            expect = set()
            for i in range(30):
                sub = os.path.join(d, f"a{i % 5}", f"b{i % 3}")
                os.makedirs(sub, exist_ok=True)
                f = os.path.join(sub, f"f{i}.tmp")
                with open(f, "w") as fh:
                    fh.write("x" * i)
                if i % 4:
                    os.utime(f, (past, past))
                    expect.add(f)
            with open(os.path.join(outside, "secret.txt"), "w") as fh:
                fh.write("no")
            os.utime(os.path.join(outside, "secret.txt"), (past, past))
            subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(d, "link"), outside], capture_output=True)
            for workers in (1, 4):
                n, size, found = cleaner._walk([d], grace=True, collect=True, workers=workers)
                self.assertEqual({p for p, _ in found}, expect)
                self.assertEqual(n, len(expect))
                self.assertEqual(size, sum(os.path.getsize(p) for p in expect))
        finally:
            subprocess.run(["cmd", "/c", "rmdir", os.path.join(d, "link")], capture_output=True)
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)

    def test_clean_never_crosses_junctions(self):
        d = tempfile.mkdtemp(prefix="mrj_")
        outside = tempfile.mkdtemp(prefix="mrj_out_")
        try:
            os.makedirs(os.path.join(outside, "empty_inside"))
            old = os.path.join(outside, "old.tmp")
            with open(old, "w") as fh:
                fh.write("x")
            past = time.time() - 10 * 86400
            os.utime(old, (past, past))
            os.makedirs(os.path.join(d, "empty", "deeper"))
            for p in (os.path.join(d, "empty", "deeper"), os.path.join(d, "empty")):
                os.utime(p, (past, past))
            subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(d, "j"), outside], capture_output=True)
            cats = {"t": {"id": "t", "label": "t", "group": "g", "globs": [d], "grace": True, "temp": True,
                          "default": True, "desc": ""}}
            with mock.patch.object(cleaner, "categories", return_value=cats):
                cleaner.clean(["t"], in_use={})
            self.assertTrue(os.path.isdir(os.path.join(outside, "empty_inside")))  # outside: untouched
            self.assertTrue(os.path.exists(old))
            self.assertFalse(os.path.exists(os.path.join(d, "empty")))            # inside: empty ones removed
            self.assertTrue(os.path.isdir(d))
        finally:
            subprocess.run(["cmd", "/c", "rmdir", os.path.join(d, "j")], capture_output=True)
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)

    def test_scan_streams_each_category(self):
        seen = []
        cats = {k: {"id": k, "label": k, "group": "g", "globs": [], "default": True, "desc": ""} for k in "abc"}
        with mock.patch.object(cleaner, "categories", return_value=cats):
            r = cleaner.scan(on_item=lambda it: seen.append(it["id"]))
        self.assertEqual(sorted(seen), ["a", "b", "c"])
        self.assertEqual([i["id"] for i in r], ["a", "b", "c"])  # the final result keeps the order

    def test_categories_are_inside_known_places(self):
        for c in cleaner.categories().values():
            for g in c.get("globs", []) + c.get("files", []):
                self.assertTrue(os.path.isabs(g), g)
                self.assertNotIn(os.path.normcase(g), {os.path.normcase(os.environ["WINDIR"]), "c:\\"})


class Speed(unittest.TestCase):
    """Speed shortcuts that must give exactly the same result as the slow path."""

    def test_folder_size_skips_junctions(self):
        from rshub.engine.removal import paths
        d = tempfile.mkdtemp(prefix="mrs_")
        outside = tempfile.mkdtemp(prefix="mrs_out_")
        try:
            os.makedirs(os.path.join(d, "x"))
            with open(os.path.join(d, "x", "a.bin"), "wb") as fh:
                fh.write(b"0" * 1000)
            with open(os.path.join(outside, "big.bin"), "wb") as fh:
                fh.write(b"0" * 50000)
            subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(d, "j"), outside], capture_output=True)
            self.assertEqual(paths.folder_size(d), (1000, 1))
        finally:
            subprocess.run(["cmd", "/c", "rmdir", os.path.join(d, "j")], capture_output=True)
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)

    def test_scanner_entries_match_isdir(self):
        from rshub.engine.removal import scanner
        d = tempfile.mkdtemp(prefix="mre_")
        gone = tempfile.mkdtemp(prefix="mre_gone_")
        try:
            os.makedirs(os.path.join(d, "dir"))
            open(os.path.join(d, "file.txt"), "w").close()
            subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(d, "broken"), gone], capture_output=True)
            os.rmdir(gone)  # broken junction: the listing says folder, isdir says no
            got = {name: isd for name, _, isd in scanner.Scanner._entries(d)}
            self.assertEqual(got, {n: os.path.isdir(os.path.join(d, n)) for n in os.listdir(d)})
            self.assertFalse(got["broken"])
        finally:
            subprocess.run(["cmd", "/c", "rmdir", os.path.join(d, "broken")], capture_output=True)
            shutil.rmtree(d, ignore_errors=True)

    def test_name_tokens_cache_returns_a_copy(self):
        from rshub.engine.removal import scanner
        p = {"name": "Xfer Records Serum 2", "publisher": "Xfer Records"}
        t1 = scanner.name_tokens(p)
        t1.clear()
        self.assertTrue(scanner.name_tokens(p))

    def test_schtasks_reader(self):
        from rshub.engine.removal import system
        out = system._tasks_schtasks()
        self.assertIsNotNone(out)
        for t in out:
            self.assertTrue(t["path"].startswith("\\"))
            self.assertFalse(t["path"].lower().startswith("\\microsoft\\"))

    def test_index_builds_each_section_once(self):
        import threading
        from rshub.engine.removal import index
        calls = []

        def slow():
            calls.append(1)
            time.sleep(0.2)
            return [1]
        index.invalidate("probe")
        ts = [threading.Thread(target=index._get, args=("probe", slow)) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(calls), 1)
        index.invalidate("probe")


class Settings(unittest.TestCase):
    def test_unknown_and_wrong_types_ignored(self):
        cur = settings.save({"theme": "light", "evil": 1, "quarantine_days": "nope"})
        self.assertEqual(cur["theme"], "light")
        self.assertNotIn("evil", cur)
        self.assertIsInstance(cur["quarantine_days"], int)
        settings.save({"theme": "system"})


if __name__ == "__main__":
    unittest.main()

"""Storage Checker and Autopilot: deleting safely, duplicates, scans, old downloads, mute and approved commands.

Everything runs in temp folders; SHFileOperation and the registry are mocked: nothing real is deleted or changed."""
import os
import shutil
import struct
import tempfile
import time
import types
import unittest
from unittest import mock

from rshub.engine import autopilot, ntfs, storage


class FakeJob:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled
        self.id = "t"
        self.lines = []

    def progress(self, *a, **k):
        if self.cancelled:                  # the user pressed Cancel while it was working
            raise RuntimeError("cancelled")

    def log(self, line):
        self.lines.append(line)

    def check(self):
        if self.cancelled:
            raise RuntimeError("cancelled")

    def stop(self):
        raise RuntimeError("cancelled")


def tmpdir(case):
    d = tempfile.mkdtemp(prefix="sh_test_")
    case.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def write(path, size=0, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if data is not None else b"x" * size)


# ------------------------------------------------------------------ storage: what can be deleted
class Protected(unittest.TestCase):
    def blocked(self, p):
        return storage._protected(p) is not None

    def test_system_and_programs(self):
        win = os.environ.get("SystemRoot", r"C:\Windows")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        for p in (win, os.path.join(win, "System32", "x.dll"), pf, os.path.join(pf, "Foo", "bin", "a.dll"),
                  os.path.join(pf, "Foo")):
            self.assertTrue(self.blocked(p), p)

    def test_user_and_data_roots(self):
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        prof = os.path.expanduser("~")
        for p in (os.path.dirname(prof), prof, pd, os.path.join(prof, "AppData"), os.path.join(prof, "Downloads"),
                  os.path.join(prof, "Documents"), "C:\\"):
            self.assertTrue(self.blocked(p), p)

    def test_other_users(self):
        other = os.path.join(os.path.dirname(os.path.expanduser("~")), "SomeoneElse", "Docs", "a.txt")
        self.assertTrue(self.blocked(other))
        self.assertIn("another user", storage._protected(other) or "")

    def test_recycle_bin_and_volume_info_of_any_drive(self):
        for p in (r"D:\$Recycle.Bin\S-1-5-21\$R1.txt", r"E:\System Volume Information\x", r"Z:\$RECYCLE.BIN"):
            self.assertTrue(self.blocked(p), p)

    def test_a_normal_file_is_allowed(self):
        d = tmpdir(self)
        f = os.path.join(d, "big.iso")
        write(f, 10)
        self.assertIsNone(storage._protected(f))
        self.assertIsNone(storage._protected(os.path.join(os.path.expanduser("~"), "Videos", "clip.mp4")))


class Recycle(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.f = os.path.join(self.d, "a.bin")
        write(self.f, 100)

    def test_no_bin_means_nothing_is_deleted(self):
        with mock.patch.object(storage, "can_recycle", return_value=(False, "drive")), \
                mock.patch.object(storage, "_shell_delete") as sd:
            r = storage.trash(self.f)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("noBin"))
        sd.assert_not_called()
        self.assertTrue(os.path.exists(self.f))

    def test_recycle_goes_with_undo(self):
        with mock.patch.object(storage, "can_recycle", return_value=(True, "")), \
                mock.patch.object(storage, "_shell_delete", return_value=True) as sd:
            r = storage.trash(self.f)
        self.assertTrue(r["ok"])
        sd.assert_called_once_with(os.path.abspath(self.f), undo=True)

    def test_permanent_only_when_asked(self):
        with mock.patch.object(storage, "can_recycle", return_value=(False, "drive")), \
                mock.patch.object(storage, "_shell_delete", return_value=True) as sd:
            self.assertFalse(storage.trash(self.f, "yes")["ok"])     # only the boolean True counts
            sd.assert_not_called()
            r = storage.trash(self.f, True)
        self.assertTrue(r["ok"] and r["permanent"])
        sd.assert_called_once_with(os.path.abspath(self.f), undo=False)

    def test_permanent_still_respects_protected(self):
        with mock.patch.object(storage, "_shell_delete") as sd:
            r = storage.trash(os.environ.get("SystemRoot", r"C:\Windows"), True)
        self.assertFalse(r["ok"])
        sd.assert_not_called()

    def test_can_recycle_rules(self):
        with mock.patch.object(storage, "_drive_type", return_value=2):          # removable
            self.assertEqual(storage.can_recycle(self.f), (False, "drive"))
        with mock.patch.object(storage, "_drive_type", return_value=3), \
                mock.patch.object(storage, "_bin_settings", return_value=(True, None)):   # bin turned off
            self.assertEqual(storage.can_recycle(self.f), (False, "off"))
        with mock.patch.object(storage, "_drive_type", return_value=3), \
                mock.patch.object(storage, "_bin_settings", return_value=(False, 50)):    # 50 bytes of quota
            self.assertEqual(storage.can_recycle(self.f), (False, "size"))
        with mock.patch.object(storage, "_drive_type", return_value=3), \
                mock.patch.object(storage, "_bin_settings", return_value=(False, 10 * 1024 * 1024)):
            self.assertEqual(storage.can_recycle(self.f), (True, ""))

    def test_restore_skips_an_odd_recycle_bin_record(self):
        # a truncated $I file (odd length) raised UnicodeDecodeError out of restore(); it must just be skipped
        import io
        target = os.path.join(self.d, "back.txt")
        name = target.encode("utf-16-le")
        good = (2).to_bytes(8, "little") + bytes(8) + (5).to_bytes(8, "little") + \
            (len(target) + 1).to_bytes(4, "little") + name + b"\0\0"
        bad = (2).to_bytes(8, "little") + bytes(16) + (40).to_bytes(4, "little") + b"\x41\x00\x42"
        files = {"$Ibad.txt": bad, "$Igood.txt": good}
        real_open = open

        def fake_listdir(p):
            return ["S-1-5-21-x"] if p.lower().endswith("$recycle.bin") else list(files)

        def fake_open(p, *a, **k):
            b = os.path.basename(str(p))
            return io.BytesIO(files[b]) if b in files else real_open(p, *a, **k)
        with mock.patch.object(storage.os, "listdir", fake_listdir), mock.patch("builtins.open", fake_open), \
                mock.patch.object(storage.os, "replace") as rep, mock.patch.object(storage.os, "remove"):
            r = storage.restore(target)
        self.assertTrue(r["ok"], r)
        self.assertTrue(rep.call_args[0][0].endswith("$Rgood.txt"))

    def test_default_bin_size_is_windows_own(self):
        # 10% of the first 40 GB + 5% of the rest: on 1 TB that is ~53 GB, not 100 GB (Windows deletes bigger items
        # for good, silently)
        gib = 1024 ** 3
        tb = 1000 * gib
        with mock.patch.object(storage, "_drive_type", return_value=3), \
                mock.patch.object(storage, "_bin_settings", return_value=(False, None)), \
                mock.patch.object(storage, "_disk_of", return_value={"total": tb}), \
                mock.patch.object(storage, "_size_up_to", side_effect=lambda p, cap: min(70 * gib, cap + 1)):
            self.assertEqual(storage.can_recycle(self.f), (False, "size"))
        with mock.patch.object(storage, "_drive_type", return_value=3), \
                mock.patch.object(storage, "_bin_settings", return_value=(False, None)), \
                mock.patch.object(storage, "_disk_of", return_value={"total": 20 * gib}), \
                mock.patch.object(storage, "_size_up_to", return_value=int(1.9 * gib)):
            self.assertEqual(storage.can_recycle(self.f), (True, ""))

    def test_size_up_to_stops_early_and_counts_folders(self):
        write(os.path.join(self.d, "sub", "b.bin"), 300)
        self.assertEqual(storage._size_up_to(self.d, 10 ** 9), 400)
        self.assertGreater(storage._size_up_to(self.d, 50), 50)


# ------------------------------------------------------------------ storage: scans and duplicates
class Scans(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        write(os.path.join(self.d, "a", "one.bin"), 2 * 1024 * 1024)
        self.prev = storage._cur["scan"]
        self.addCleanup(lambda: storage._cur.__setitem__("scan", self.prev))
        p = mock.patch.object(storage, "DATA", os.path.join(self.d, "_snap"))
        p.start()
        self.addCleanup(p.stop)

    def test_private_scan_is_not_published(self):
        storage._cur["scan"] = None
        s = storage.scan_private(FakeJob(), self.d)
        self.assertIsNone(storage._cur["scan"])
        self.assertEqual(s.files, 1)

    def test_published_scan_survives_a_cancelled_one(self):
        storage.scan(FakeJob(), self.d)
        pub = storage._cur["scan"]
        self.assertIsNotNone(pub)
        with self.assertRaises(RuntimeError):
            storage.scan(FakeJob(cancelled=True), self.d)
        self.assertIs(storage._cur["scan"], pub)
        self.assertIsNotNone(storage.info())

    def test_published_scan_stays_while_a_new_one_runs(self):
        storage.scan(FakeJob(), self.d)
        pub = storage._cur["scan"]
        seen = []
        real = storage._scan_locked

        def spy(job, target, threads):
            seen.append(storage._cur["scan"])
            return real(job, target, threads)
        with mock.patch.object(storage, "_scan_locked", spy):
            storage.scan(FakeJob(), self.d)
        self.assertIs(seen[0], pub)                 # still there while scanning
        self.assertIsNot(storage._cur["scan"], pub)  # swapped when done

    def test_opening_shows_the_last_scan_without_scanning(self):
        """2.0.0: the Storage Checker no longer scans on opening. It shows the scan this process already has
        (published) or the date and size of the last one (last_scan, from the Compare snapshot, which Autopilot's
        private scans also write); neither reads the disk."""
        storage._cur["scan"] = None
        self.assertIsNone(storage.published())
        with mock.patch.object(storage, "_scan_locked") as sl:
            self.assertIsNone(storage.last_scan(self.d))                   # never scanned
            sl.assert_not_called()
        t0 = time.time()
        storage.scan_private(FakeJob(), self.d)                             # Autopilot's: not published, but dated
        self.assertIsNone(storage.published())
        last = storage.last_scan(self.d)
        self.assertEqual((last["files"], last["size"]), (1, 2 * 1024 * 1024))
        self.assertGreaterEqual(last["when"], int(t0 * 1000) - 1000)
        self.assertEqual(os.path.normcase(last["target"]), os.path.normcase(os.path.abspath(self.d)))
        r = storage.scan(FakeJob(), self.d)
        pub = storage.published()
        self.assertEqual(pub["info"]["files"], r["info"]["files"])
        self.assertEqual(pub["root"]["id"], r["root"]["id"])
        with open(os.path.join(storage.DATA, storage._key(os.path.abspath(self.d)) + ".json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(storage.last_scan(self.d))                       # a damaged snapshot: no date, no error

    def test_duplicates_skip_programs_and_games(self):
        # the temp folder itself lives under AppData, which the real rules skip: leave that marker out for this test
        for p in (mock.patch.object(storage, "SKIP_PROGRAMS", tuple(x for x in storage.SKIP_PROGRAMS if x != "\\appdata\\")),
                  mock.patch.dict(os.environ, {"APPDATA": "Z:\\none", "LOCALAPPDATA": "Z:\\none"})):
            p.start()
            self.addCleanup(p.stop)
        blob = os.urandom(1100 * 1024)
        for rel in ("docs\\a.bin", "docs\\b.bin", "Steam\\steamapps\\common\\G\\x.dll", "Steam\\steamapps\\common\\H\\x.dll",
                    "Program Files\\App\\y.dll", "Program Files\\App2\\y.dll"):
            write(os.path.join(self.d, rel), data=blob)
        s = storage.scan_private(FakeJob(), self.d)
        res = storage._duplicates(FakeJob(), 1, s)
        self.assertEqual(len(res["groups"]), 1)
        self.assertEqual(len(res["groups"][0]["copies"]), 2)
        self.assertTrue(all("docs" in c["path"] for c in res["groups"][0]["copies"]))
        self.assertEqual(res["skippedPrograms"], 4)

    def test_dup_skipped_paths(self):
        for p in (r"C:\Program Files\X\a.dll", r"C:\Program Files (x86)\X\a.dll", r"C:\ProgramData\X\a.dll",
                  r"C:\Users\u\AppData\Local\X\a.dll", r"D:\SteamLibrary\steamapps\common\g\a.pak",
                  r"D:\Epic Games\G\a.pak", r"E:\GOG Games\G\a.pak", r"D:\XboxGames\G\a.pak", r"C:\Windows\a.dll"):
            self.assertTrue(storage._dup_skipped(p), p)
        self.assertFalse(storage._dup_skipped(r"C:\Users\u\Videos\a.mp4"))


# ------------------------------------------------------------------ ntfs
class Ntfs(unittest.TestCase):
    class Strict4K:
        """A raw volume on a 4K-native disk: only reads in whole sectors."""

        def __init__(self, boot):
            self.boot = boot

        def seek(self, n):
            pass

        def readinto(self, b):
            if len(b) % 4096:
                raise OSError(22, "invalid argument")
            b[:len(self.boot)] = self.boot
            return len(b)

    def test_boot_sector_on_4k_native_volume(self):
        boot = bytearray(4096)
        boot[3:11] = b"NTFS    "
        struct.pack_into("<H", boot, 0x0B, 4096)
        boot[0x0D] = 1
        struct.pack_into("<q", boot, 0x30, 786432)
        boot[0x40] = 0xF6                      # records of 2**10 bytes
        sector, cluster, lcn, rec = ntfs._boot(self.Strict4K(boot))
        self.assertEqual((sector, cluster, lcn, rec), (4096, 4096, 786432, 1024))

    def test_segments_with_clusters_smaller_than_a_record(self):
        # 512-byte clusters, 1 KB records; the first run (3 clusters) ends in the middle of record 1
        lay = {"rec": 1024, "cluster": 512, "runs": [(100, 3), (200, 3)]}
        self.assertEqual(ntfs._segments(lay, 0, 3), [(100 * 512, 1024, 0), (200 * 512 + 512, 1024, 2)])
        self.assertEqual(ntfs._segments(lay, 2, 3), [(200 * 512 + 512, 1024, 2)])   # record 2 keeps its number

    def test_segments_with_normal_clusters_are_unchanged(self):
        lay = {"rec": 1024, "cluster": 4096, "runs": [(10, 2), (None, 1), (50, 3)]}
        self.assertEqual(ntfs._segments(lay, 0, 20), [(10 * 4096, 8 * 1024, 0), (50 * 4096, 8 * 1024, 12)])
        self.assertEqual(ntfs._segments(lay, 5, 14), [(10 * 4096 + 5 * 1024, 3 * 1024, 5), (50 * 4096, 2 * 1024, 12)])


# ------------------------------------------------------------------ autopilot
class Approval(unittest.TestCase):
    def act(self, a, admin, approved=()):
        with mock.patch.object(autopilot, "_admin", return_value=admin), \
                mock.patch.object(autopilot, "_approved_load", return_value=set(approved)), \
                mock.patch.object(autopilot.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(stdout=b"", returncode=0)
            job = FakeJob()
            out = autopilot._act(job, a, {"after": {}})
        return out, run, job

    def test_hash_is_stable_and_specific(self):
        a = {"k": "cmd", "p": {"cmd": "echo hi", "as": "user"}}
        h = autopilot._action_hash(a)
        self.assertEqual(len(h), 64)
        self.assertEqual(h, autopilot._action_hash({"k": "cmd", "p": {"cmd": "  echo hi ", "as": "user"}}))
        self.assertNotEqual(h, autopilot._action_hash({"k": "cmd", "p": {"cmd": "echo hi", "as": "admin"}}))
        self.assertNotEqual(h, autopilot._action_hash({"k": "cmd", "p": {"cmd": "echo hi!", "as": "user"}}))
        self.assertNotEqual(autopilot._action_hash({"k": "openApp", "p": {"path": "C:\\a.exe"}}),
                            autopilot._action_hash({"k": "openApp", "p": {"path": "C:\\b.exe"}}))
        self.assertIsNone(autopilot._action_hash({"k": "clean", "p": {}}))

    def test_elevated_unapproved_command_is_skipped(self):
        out, run, job = self.act({"k": "cmd", "p": {"cmd": "echo hi"}}, admin=True)
        self.assertEqual(out[0], "skip")
        run.assert_not_called()
        self.assertIn("save this routine again", autopilot._en(out[1]))
        self.assertTrue(job.lines)

    def test_elevated_unapproved_open_app_is_skipped(self):
        with mock.patch.object(autopilot.os, "startfile", create=True) as sf:
            out, _run, _j = self.act({"k": "openApp", "p": {"path": __file__}}, admin=True)
        self.assertEqual(out[0], "skip")
        sf.assert_not_called()

    def test_approved_command_runs(self):
        a = {"k": "cmd", "p": {"cmd": "echo hi", "as": "admin"}}
        out, run, _j = self.act(a, admin=True, approved={autopilot._action_hash(a)})
        self.assertEqual(out[0], "ok")
        run.assert_called_once()

    def test_not_elevated_behaves_as_before(self):
        out, run, _j = self.act({"k": "cmd", "p": {"cmd": "echo hi"}}, admin=False)
        self.assertEqual(out[0], "ok")
        run.assert_called_once()

    def test_approve_only_while_elevated(self):
        r = {"acts": [{"k": "cmd", "p": {"cmd": "echo hi"}}, {"k": "clean", "p": {}},
                      {"k": "openApp", "p": {"path": "C:\\x.exe"}}]}
        with mock.patch.object(autopilot, "_admin", return_value=False), \
                mock.patch.object(autopilot, "_approved_add") as add:
            self.assertFalse(autopilot.approve([r])["ok"])
            add.assert_not_called()
        with mock.patch.object(autopilot, "_admin", return_value=True), \
                mock.patch.object(autopilot, "_approved_add", return_value=True) as add:
            res = autopilot.approve([r])
        self.assertEqual(res["n"], 2)
        self.assertEqual(add.call_args[0][0], {autopilot._action_hash(r["acts"][0]), autopilot._action_hash(r["acts"][2])})

    def test_dry_run_mentions_it(self):
        r = {"id": "x", "acts": [{"k": "cmd", "p": {"cmd": "echo hi"}}]}
        with mock.patch.object(autopilot, "_admin", return_value=True), \
                mock.patch.object(autopilot, "_approved_load", return_value=set()):
            d = autopilot.dry_run(r)
        self.assertIn("not approved", d["acts"][0]["noteM"]["t"])


class DiskScanAction(unittest.TestCase):
    def fake_storage(self, cmp_):
        st = mock.Mock()
        st.scan_private.return_value = object()
        st._compare.return_value = cmp_
        return st

    def run_scan(self, cmp_, gb=10):
        st = self.fake_storage(cmp_)
        with mock.patch.object(autopilot, "_storage", return_value=st):
            out = autopilot._act(FakeJob(), {"k": "scanDisk", "p": {"drive": "D:", "gb": gb}}, {})
        return out, st

    def test_grew_bytes_reads_lists_and_dicts(self):
        G = 1024 ** 3
        self.assertEqual(autopilot._grew_bytes({"changes": [["C:\\a", 3 * G, 10], ["C:\\b", -G, -1], ["C:\\c", G, 1]]}), 4 * G)
        self.assertEqual(autopilot._grew_bytes({"changes": [{"delta": 2 * G}, {"delta": -5}]}), 2 * G)
        self.assertEqual(autopilot._grew_bytes({"changes": [], "delta": 7 * G}), 7 * G)
        self.assertEqual(autopilot._grew_bytes({"prev": 1}), 0)

    def test_big_growth_warns(self):
        G = 1024 ** 3
        out, st = self.run_scan({"prev": 1, "changes": [["D:\\x", 20 * G, 5]], "delta": 20 * G})
        self.assertEqual(out[0], "decide")
        self.assertIn("grew", autopilot._en(out[1]))
        st.scan_private.assert_called_once()               # private scan, never published
        st._compare.assert_called_once()

    def test_small_growth_is_ok(self):
        out, _st = self.run_scan({"prev": 1, "changes": [["D:\\x", 1024 ** 3, 5]]})
        self.assertEqual(out[0], "ok")

    def test_duplicates_action_uses_a_private_scan(self):
        st = mock.Mock()
        st.scan_private.return_value = object()
        st._duplicates.return_value = {"groups": [], "wasted": 0}
        with mock.patch.object(autopilot, "_storage", return_value=st):
            out = autopilot._act(FakeJob(), {"k": "dupes", "p": {"min": 50}}, {})
        self.assertEqual(out[0], "ok")
        st.scan_private.assert_called_once()
        self.assertIs(st._duplicates.call_args[0][2], st.scan_private.return_value)


class OldDownloads(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir(self)
        self.dl = os.path.join(self.base, "Downloads")
        os.makedirs(self.dl)
        kf = {"Profile": self.base, "Documents": os.path.join(self.base, "Documents"),
              "Desktop": os.path.join(self.base, "Desktop"), "Downloads": self.dl,
              "OneDrive": os.path.join(self.base, "OneDrive")}
        p = mock.patch("rshub.engine.removal.paths.known_folders", return_value=kf)
        p.start()
        self.addCleanup(p.stop)

    def test_downloads_ok_refuses_wrong_folders(self):
        self.assertTrue(autopilot._downloads_ok(self.dl))
        os.makedirs(os.path.join(self.base, "Documents"))
        for bad in (self.base, os.path.join(self.base, "Documents"), os.path.splitdrive(self.base)[0] + "\\"):
            self.assertFalse(autopilot._downloads_ok(bad), bad)
        self.assertFalse(autopilot._downloads_ok(os.path.join(self.base, "missing")))

    def test_stamp_uses_the_latest_time(self):
        st = types.SimpleNamespace(st_mtime=100, st_ctime=900, st_birthtime=500)
        self.assertEqual(autopilot._stamp(st), 900)
        self.assertEqual(autopilot._stamp(types.SimpleNamespace(st_mtime=100, st_ctime=50)), 100)

    def test_folder_is_as_new_as_its_newest_item(self):
        sub = os.path.join(self.dl, "pack")
        write(os.path.join(sub, "old.txt"), 1)
        write(os.path.join(sub, "deep", "new.txt"), 1)
        os.utime(os.path.join(sub, "old.txt"), (1000, 1000))
        os.utime(os.path.join(sub, "deep", "new.txt"), (5000, 5000))
        os.utime(os.path.join(sub, "deep"), (1000, 1000))
        os.utime(sub, (1000, 1000))
        with mock.patch.object(autopilot, "_stamp", lambda st: st.st_mtime):
            self.assertEqual(autopilot._newest_activity(sub, os.stat(sub)), 5000)
            # a folder with more items than the cap counts as recent: it is never judged old
            self.assertGreater(autopilot._newest_activity(sub, os.stat(sub), cap=1), time.time() - 60)

    def test_copied_file_with_old_mtime_is_recent(self):
        f = os.path.join(self.dl, "copied.zip")
        write(f, 5)
        os.utime(f, (1000, 1000))                     # an old modified time, but it was created just now
        self.assertGreater(autopilot._newest_activity(f, os.stat(f)), time.time() - 3600)

    def run_action(self, calls):
        with mock.patch.object(autopilot, "_downloads_dir", return_value=self.dl), \
                mock.patch.object(autopilot, "_call", side_effect=calls) as call:
            out = autopilot._act(FakeJob(), {"k": "oldDl", "p": {"days": 30}}, {})
        return out, call

    def test_only_old_items_go_and_without_a_bin_they_stay(self):
        for n in ("old1.iso", "old2.iso", "fresh.iso"):
            write(os.path.join(self.dl, n), 3)
        old = time.time() - 90 * 86400
        with mock.patch.object(autopilot, "_newest_activity", side_effect=lambda p, st, cap=5000: time.time() if "fresh" in p else old):
            out, call = self.run_action(lambda mod, fn, path, *a: {"ok": True} if "old1" in path else {"ok": False, "noBin": True})
        sent = [c.args for c in call.call_args_list]
        self.assertEqual(len(sent), 2)                                   # fresh.iso was never offered
        self.assertTrue(all(a[:2] == ("storage", "trash") and len(a) == 3 for a in sent))   # never permanent
        self.assertEqual(out[0], "skip")
        self.assertEqual(out[1]["v"], {"n": 1, "k": 1})

    def test_unsafe_folder_is_refused(self):
        with mock.patch.object(autopilot, "_downloads_dir", return_value=self.base), \
                mock.patch.object(autopilot, "_call") as call:
            out = autopilot._act(FakeJob(), {"k": "oldDl", "p": {}}, {})
        self.assertEqual(out[0], "skip")
        call.assert_not_called()


class Mute(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        for p in (mock.patch.object(autopilot, "_data_dir", return_value=self.d),
                  mock.patch.object(autopilot, "_toast_set")):
            m = p.start()
            self.addCleanup(p.stop)
        self.set = autopilot._toast_set
        self.addCleanup(lambda: autopilot._mute_timer and autopilot._mute_timer.cancel())

    def mute(self, current):
        with mock.patch.object(autopilot, "_toast_get", return_value=current):
            return autopilot._mute(60)

    def test_deadline_and_old_value_are_saved(self):
        out = self.mute(1)
        self.assertEqual(out[0], "ok")
        self.set.assert_called_with(0)
        saved = autopilot._load(autopilot._mute_file(), None)
        self.assertEqual(saved["prev"], 1)
        self.assertGreater(saved["until"], time.time() + 3000)

    def test_not_restored_before_the_deadline_but_after_a_restart_it_is(self):
        self.mute(1)
        self.set.reset_mock()
        self.assertFalse(autopilot.restore_mute())
        self.set.assert_not_called()
        saved = autopilot._load(autopilot._mute_file(), None)
        saved["until"] = time.time() - 5                    # the app was closed and the time passed
        autopilot._save(autopilot._mute_file(), saved)
        self.assertTrue(autopilot.restore_mute())           # what start() / the next check does
        self.set.assert_called_with(1)
        self.assertFalse(os.path.exists(autopilot._mute_file()))

    def test_user_who_had_them_off_keeps_them_off(self):
        self.mute(0)
        self.set.reset_mock()
        autopilot.restore_mute(force=True)
        self.set.assert_called_with(0)

    def test_value_that_did_not_exist_is_removed_again(self):
        self.mute(None)
        self.set.reset_mock()
        autopilot.restore_mute(force=True)
        self.set.assert_called_with(None)

    def test_second_mute_keeps_the_original_value(self):
        self.mute(1)
        self.mute(0)                                         # now it reads 0 because the first mute set it
        self.assertEqual(autopilot._load(autopilot._mute_file(), None)["prev"], 1)

    def test_failed_restore_is_retried(self):
        self.mute(1)
        autopilot._toast_set.side_effect = OSError("denied")
        self.assertFalse(autopilot.restore_mute(force=True))
        self.assertTrue(os.path.exists(autopilot._mute_file()))


class NetStep(unittest.TestCase):
    """A routine never runs the full network reset (it erases static IP and DNS settings); the DNS flush still does."""

    def act(self, kind, code=0):
        with mock.patch.object(autopilot.subprocess, "run", return_value=types.SimpleNamespace(stdout=b"", returncode=code)) as run,                 mock.patch.object(autopilot, "_job") as job_fn:
            job = FakeJob()
            out = autopilot._act(job, {"k": "net", "p": {"kind": kind}}, {"after": {}})
        return out, run, job_fn, job

    def test_full_is_skipped_and_never_resets(self):
        out, run, job_fn, job = self.act("full")
        job_fn.assert_not_called()
        self.assertEqual(out[0], "skip")
        self.assertIn("Run it from Super Optimizer", autopilot._en(out[1]))
        self.assertEqual(run.call_args[0][0], ["ipconfig", "/flushdns"])
        self.assertTrue(any("never run by a routine" in l for l in job.lines))

    def test_full_with_failed_flush_is_still_skipped(self):
        out, _run, job_fn, _job = self.act("full", code=1)
        job_fn.assert_not_called()
        self.assertEqual(out[0], "skip")

    def test_dns_only_flushes(self):
        out, run, job_fn, _job = self.act("dns")
        job_fn.assert_not_called()
        self.assertEqual(out[0], "ok")
        self.assertEqual(autopilot._en(out[1]), "DNS cache flushed")


if __name__ == "__main__":
    unittest.main()

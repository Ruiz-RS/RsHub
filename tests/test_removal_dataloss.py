"""Data-loss regressions of the removal engine: unsafe temp roots, junctions and files left for the next restart.

Everything is built inside temporary folders; nothing real is cleaned, uninstalled or scheduled.
"""
import _winapi
import os
import shutil
import tempfile
import unittest
from unittest import mock

from rshub.engine.removal import cleaner, paths, quarantine, safety, system


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _junction(target, link):
    _winapi.CreateJunction(target, link)


class TempRootSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sh_troot_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.profile = os.path.join(self.tmp, "Users", "bob")
        self.docs = os.path.join(self.profile, "Documents")
        for d in (self.docs, os.path.join(self.profile, "Downloads"), os.path.join(self.profile, "AppData", "Local", "Temp")):
            os.makedirs(d)
        real = dict(paths.known_folders())
        real.update(Profile=self.profile, Documents=self.docs, Downloads=os.path.join(self.profile, "Downloads"),
                    LocalAppData=os.path.join(self.profile, "AppData", "Local"))
        p = mock.patch.object(paths, "known_folders", return_value=real)
        p.start()
        self.addCleanup(p.stop)

    def test_refused_roots(self):
        drive = os.path.splitdrive(self.tmp)[0] + "\\"
        for bad in (drive, self.profile, os.path.dirname(self.profile), self.docs,
                    os.path.join(self.profile, "Downloads"), None, "", "Temp", os.path.join(self.tmp, "nope")):
            self.assertTrue(safety.temp_root_problem(bad), bad)

    def test_inside_a_personal_folder_refused(self):
        sub = os.path.join(self.docs, "work")
        os.makedirs(sub)
        self.assertTrue(safety.temp_root_problem(sub))

    def test_normal_temp_accepted(self):
        self.assertIsNone(safety.temp_root_problem(os.path.join(self.profile, "AppData", "Local", "Temp")))

    def test_junction_root_refused(self):
        link = os.path.join(self.tmp, "Temp")
        _junction(self.docs, link)
        self.addCleanup(os.rmdir, link)
        self.assertTrue(safety.temp_root_problem(link))

    def test_cleaner_skips_category_and_keeps_user_data(self):
        old = os.path.join(self.docs, "thesis.docx")
        _write(old)
        os.utime(old, (1, 1))
        with mock.patch.object(paths, "user_temp_dir", return_value=self.profile):   # TEMP pointing at the profile
            cat = cleaner.categories()["user_temp"]
            self.assertTrue(cat["refused"])
            self.assertEqual(cat["globs"], [])
            self.assertFalse(cat["default"])
            self.assertEqual(cleaner._expand(cat), ([], []))
            cleaner.clean(["user_temp"], in_use={})
        self.assertTrue(os.path.exists(old))

    def test_drive_root_temp_is_refused_in_scan(self):
        drive = os.path.splitdrive(self.tmp)[0] + "\\"
        with mock.patch.object(paths, "user_temp_dir", return_value=drive), \
                mock.patch.object(cleaner, "in_use_paths", return_value={}):
            self.assertTrue(cleaner.categories()["user_temp"]["refused"])
            item = next(i for i in cleaner.scan() if i["id"] == "user_temp")
        self.assertTrue(item["skipped"])
        self.assertTrue(item["skipReason"])
        self.assertEqual(item["size"], 0)
        self.assertFalse(item["default"])

    def test_empty_known_folder_never_becomes_relative_glob(self):
        self.assertFalse(cleaner._absolute(os.path.join("", "Google", "Chrome")))
        self.assertTrue(cleaner._absolute(os.path.join(self.tmp, "x")))


class Junctions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sh_junc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = os.path.join(self.tmp, "precious")
        _write(os.path.join(self.target, "keep.txt"), b"keep")
        _write(os.path.join(self.target, "deep", "keep2.txt"), b"keep")
        os.makedirs(os.path.join(self.target, "emptydir"))
        self.tree = os.path.join(self.tmp, "app")
        _write(os.path.join(self.tree, "a.txt"))
        _write(os.path.join(self.tree, "sub", "b.txt"))
        self.link = os.path.join(self.tree, "sub", "link")
        _junction(self.target, self.link)
        self.result = {"reboot": 0, "reboot_unrestorable": 0, "_pending": []}

    def assertTargetIntact(self):
        self.assertTrue(os.path.isfile(os.path.join(self.target, "keep.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "deep", "keep2.txt")))
        self.assertTrue(os.path.isdir(os.path.join(self.target, "emptydir")))

    def test_walk_nolinks_reports_but_does_not_enter(self):
        seen = list(paths.walk_nolinks(self.tree))
        flat = [f for _d, files, _dd, _l in seen for f in files]
        self.assertFalse(any("keep" in f for f in flat))
        self.assertIn(self.link, [lk for *_x, links in seen for lk in links])

    def test_delete_tree_fallback_does_not_touch_target(self):
        with mock.patch.object(quarantine, "_rmtree", side_effect=OSError("locked")):
            quarantine._delete_tree(self.tree, self.result)
        self.assertFalse(os.path.exists(self.tree))
        self.assertTargetIntact()

    def test_remove_empty_dirs_does_not_enter_junction(self):
        quarantine._remove_empty_dirs(self.tree, self.result)
        self.assertTrue(os.path.isdir(os.path.join(self.target, "emptydir")))
        self.assertTargetIntact()

    def test_move_tree_fallback_does_not_touch_target(self):
        dst = os.path.join(self.tmp, "q", "app")
        real_replace = os.replace

        def replace(a, b):
            if os.path.normcase(a) == os.path.normcase(self.tree):
                raise OSError("tree is busy")
            return real_replace(a, b)
        with mock.patch.object(os, "replace", side_effect=replace), \
                mock.patch.object(shutil, "move", side_effect=OSError("busy")):
            quarantine._move_tree(self.tree, dst, self.result)
        self.assertTrue(os.path.isfile(os.path.join(dst, "a.txt")))
        self.assertTargetIntact()
        self.assertFalse(os.path.exists(self.tree))


class LockedFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sh_lock_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "prog", "locked.dll")
        _write(self.src)
        self.dst = os.path.join(self.tmp, "q", "files", "0_prog")
        self.result = {"reboot": 0, "reboot_unrestorable": 0, "_pending": []}

    def _lock(self):
        real = os.replace

        def replace(a, b):
            if os.path.normcase(a) == os.path.normcase(self.src):
                raise PermissionError("in use")
            return real(a, b)
        return (mock.patch.object(os, "replace", side_effect=replace),
                mock.patch.object(shutil, "copy2", side_effect=PermissionError("in use")),
                mock.patch.object(shutil, "move", side_effect=PermissionError("in use")))

    def test_same_volume_uses_delayed_move_and_stays_restorable(self):
        a, b, c = self._lock()
        with a, b, c, mock.patch.object(quarantine, "_move_on_reboot", return_value=True) as mv, \
                mock.patch.object(system, "delete_on_reboot", return_value=True) as de:
            quarantine._move_tree(self.src, self.dst, self.result)
        mv.assert_called_once_with(self.src, self.dst)
        de.assert_not_called()
        self.assertEqual(self.result["reboot"], 1)
        self.assertEqual(self.result["reboot_unrestorable"], 0)
        self.assertEqual(self.result["_pending"], [[self.src, self.dst]])

    def test_other_volume_marks_unrestorable(self):
        a, b, c = self._lock()
        with a, b, c, mock.patch.object(quarantine, "_same_volume", return_value=False), \
                mock.patch.object(system, "delete_on_reboot", return_value=True):
            quarantine._move_tree(self.src, self.dst, self.result)
        self.assertEqual(self.result["reboot_unrestorable"], 1)
        self.assertEqual(self.result["_pending"], [])

    def test_nothing_can_be_scheduled_raises(self):
        a, b, c = self._lock()
        with a, b, c, mock.patch.object(quarantine, "_move_on_reboot", return_value=False), \
                mock.patch.object(system, "delete_on_reboot", return_value=False):
            with self.assertRaises(OSError):
                quarantine._move_tree(self.src, self.dst, self.result)

    def _remove(self, same_volume):
        data = os.path.join(self.tmp, "data")
        prog = {"id": "t", "name": "Locked Test", "location": os.path.dirname(self.src)}
        item = {"kind": "file", "target": self.src, "display": self.src}
        a, b, c = self._lock()
        with mock.patch.object(paths, "DATA_DIR", data), a, b, c, \
                mock.patch.object(quarantine, "_same_volume", return_value=same_volume), \
                mock.patch.object(quarantine, "_move_on_reboot", return_value=True), \
                mock.patch.object(system, "delete_on_reboot", return_value=True), \
                mock.patch.object(quarantine.safety.Context, "check_path", return_value=(None, None)), \
                mock.patch.object(quarantine.index, "invalidate"):
            r = quarantine.remove(prog, [item], [prog])
            s = next(x for x in quarantine.sessions() if x["id"] == r["session"])
        return r, s

    def test_result_and_session_flag_pending_restart(self):
        r, s = self._remove(True)
        self.assertEqual(r["failed"], 0, r["errors"])
        self.assertTrue(r["restart_required"])
        self.assertEqual(r["reboot_unrestorable"], 0)
        self.assertTrue(s["restorable"])
        self.assertEqual(s["pendingRestart"], 1)
        self.assertTrue(s["items"][0]["pendingRestart"])

    def test_unrestorable_session_does_not_claim_restore(self):
        r, s = self._remove(False)
        self.assertTrue(r["restart_required"])
        self.assertEqual(r["reboot_unrestorable"], 1)
        self.assertFalse(s["restorable"])
        self.assertFalse(s["items"][0]["restorable"])
        self.assertEqual(s["unrestorableFiles"], 1)


if __name__ == "__main__":
    unittest.main()

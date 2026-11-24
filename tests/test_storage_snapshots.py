"""Storage Checker's snapshots (Last scan, Compare): per user, one file per target, never blocked by a damaged file."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from rshub.engine import storage
from tests.test_storage_autopilot import FakeJob, write


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sh-snap-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.data, self.legacy = os.path.join(self.d, "_data"), os.path.join(self.d, "_legacy")
        for name, val in (("DATA", self.data), ("_LEGACY", self.legacy)):
            p = mock.patch.object(storage, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.prev = storage._cur["scan"]
        self.addCleanup(lambda: storage._cur.__setitem__("scan", self.prev))
        self.t = os.path.join(self.d, "target")
        write(os.path.join(self.t, "a", "one.bin"), 2 * 1024 * 1024)

    def test_lookalike_folders_get_their_own_files(self):
        keys = {storage._key(p) for p in (r"C:\Users\a b", r"C:\Users\a-b", r"C:\Users\a_b")}
        self.assertEqual(len(keys), 3)
        self.assertEqual(storage._key(r"C:\Users\A B"), storage._key(r"c:\users\a b"))  # same folder, any case

    def test_a_damaged_snapshot_does_not_stop_the_next_one(self):
        os.makedirs(self.data)
        with open(os.path.join(self.data, storage._key(self.t) + ".json"), "w", encoding="utf-8") as fh:
            fh.write("")  # a crash while it was written
        storage.scan(FakeJob(), self.t)
        last = storage.last_scan(self.t)
        self.assertIsNotNone(last)
        self.assertEqual(storage.compare().get("prev"), None)  # nothing to compare with yet, and no error

    def test_compare_after_two_scans(self):
        storage.scan(FakeJob(), self.t)
        write(os.path.join(self.t, "b", "two.bin"), 3 * 1024 * 1024)
        storage.scan(FakeJob(), self.t)
        with open(os.path.join(self.data, storage._key(self.t) + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data["history"]), 2)
        self.assertIsNotNone(data["prev"])

    def test_the_old_programdata_snapshot_is_brought_over_once(self):
        os.makedirs(self.legacy)
        old = {"last": {"v": 2, "target": self.t, "when": 1_700_000_000, "size": 5, "files": 1, "dirs": {}, "big": []},
               "prev": None, "history": [[1_700_000_000, 5]]}
        with open(os.path.join(self.legacy, storage._old_key(self.t) + ".json"), "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        last = storage.last_scan(self.t)
        self.assertEqual(last["when"], 1_700_000_000_000)
        self.assertTrue(os.path.exists(os.path.join(self.data, storage._key(self.t) + ".json")))

    def test_an_old_file_of_another_folder_is_not_taken(self):
        os.makedirs(self.legacy)
        other = self.t + "-x"  # same old file name ("..._target_x" vs "..._target x")
        old = {"last": {"v": 2, "target": other, "when": 1_700_000_000, "size": 5, "files": 1}}
        with open(os.path.join(self.legacy, storage._old_key(self.t + " x") + ".json"), "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        self.assertIsNone(storage.last_scan(self.t + " x"))


if __name__ == "__main__":
    unittest.main()

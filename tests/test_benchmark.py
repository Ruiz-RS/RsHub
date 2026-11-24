"""Benchmark: score math, the history file and the disk test's temporary file (always deleted)."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from rshub.engine import benchmark as B
from rshub.engine.jobs import Cancelled, Job


class QuietJob(Job):
    """A job that sends nothing to a UI; cancel_after = number of progress calls before it reports a cancel."""

    def __init__(self, cancel_after=None):
        super().__init__("test")
        self.calls = 0
        self.cancel_after = cancel_after

    def _send(self, kind):
        pass

    def progress(self, *a, **k):
        self.calls += 1
        if self.cancel_after is not None and self.calls >= self.cancel_after:
            self.cancelled = True
        super().progress(*a, **k)


class Scores(unittest.TestCase):
    def test_reference_is_1000(self):
        self.assertEqual(B.score_cpu_part(B.REF["cpu_st"], "st"), 1000)
        self.assertEqual(B.score_cpu_part(B.REF["cpu_mt"], "mt"), 1000)
        r = B.REF["mem"]
        self.assertEqual(B.score_memory(r["copy"], r["read"], r["write"], r["latency"]), 1000)
        disk = {"seq_q8": {"read": B.REF["disk"]["seq_q8_read"], "write": B.REF["disk"]["seq_q8_write"]},
                "rnd_q32": {"read": B.REF["disk"]["rnd_q32_read"]},
                "rnd_q1": {"read": B.REF["disk"]["rnd_q1_read"], "write": B.REF["disk"]["rnd_q1_write"]}}
        self.assertEqual(B.score_disk(disk), 1000)
        self.assertEqual(B.score_gpu(B.REF["gpu"]["fill"], B.REF["gpu"]["geo"]), 1000)
        self.assertEqual(B.overall({"cpu": 1000, "gpu": 1000, "memory": 1000, "disk": 1000}), 1000)

    def test_twice_as_fast_is_twice_the_points(self):
        st = {k: v * 2 for k, v in B.REF["cpu_st"].items()}
        self.assertEqual(B.score_cpu_part(st, "st"), 2000)
        self.assertEqual(B.score_gpu(B.REF["gpu"]["fill"] * 2, B.REF["gpu"]["geo"] * 2), 2000)
        self.assertEqual(B.score_cpu(2000, 2000), 2000)
        r = B.REF["mem"]
        self.assertEqual(B.score_memory(r["copy"] * 2, r["read"] * 2, r["write"] * 2, r["latency"] / 2), 2000)

    def test_lower_latency_scores_higher(self):
        r = B.REF["mem"]
        fast = B.score_memory(r["copy"], r["read"], r["write"], r["latency"] * .5)
        slow = B.score_memory(r["copy"], r["read"], r["write"], r["latency"] * 2)
        self.assertGreater(fast, 1000)
        self.assertLess(slow, 1000)

    def test_overall_weights_and_missing_parts(self):
        # a missing component is skipped and the rest renormalized
        self.assertEqual(B.overall({"cpu": 1500, "gpu": None, "memory": 1500, "disk": 1500}), 1500)
        # the graphics card weighs more than memory
        a = B.overall({"cpu": 1000, "gpu": 2000, "memory": 1000, "disk": 1000})
        b = B.overall({"cpu": 1000, "gpu": 1000, "memory": 2000, "disk": 1000})
        self.assertGreater(a, b)
        self.assertIsNone(B.overall({}))
        self.assertIsNone(B.overall("nope"))

    def test_nothing_measured_gives_none(self):
        self.assertIsNone(B.score_gpu(0, 0))
        self.assertIsNone(B.score_memory(None, None, None))
        self.assertIsNone(B.score_disk({}))


class History(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shb_")
        self.p = mock.patch.object(B, "HISTORY", os.path.join(self.dir, "sub", "benchmarks.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_empty_when_missing(self):
        self.assertEqual(B.history(), [])

    def test_save_delete_clear(self):
        a = B.save_run({"kind": "cpu", "score": 900, "at": 1000})
        b = B.save_run({"kind": "full", "score": 1100, "at": 2000})
        self.assertTrue(a["id"] and b["id"] and a["id"] != b["id"])
        self.assertEqual([r["score"] for r in B.history()], [1100, 900])  # newest first
        self.assertEqual(B.delete_run(a["id"]), 1)
        self.assertEqual(B.delete_run("missing"), 0)
        self.assertEqual(len(B.history()), 1)
        self.assertEqual(B.clear_history(), 1)
        self.assertEqual(B.history(), [])
        with open(B.HISTORY, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["runs"], [])

    def test_save_sets_time(self):
        r = B.save_run({"kind": "gpu"})
        self.assertGreater(r["at"], 1.6e12)

    def test_invalid_run(self):
        for bad in (None, [], {"score": 1}, {"kind": 5}):
            with self.assertRaises(ValueError):
                B.save_run(bad)

    def test_damaged_file_is_kept_aside(self):
        os.makedirs(os.path.dirname(B.HISTORY))
        with open(B.HISTORY, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(B.history(), [])
        self.assertTrue(os.path.exists(B.HISTORY + ".bad"))
        B.save_run({"kind": "cpu"})
        self.assertEqual(len(B.history()), 1)

    def test_capped(self):
        with mock.patch.object(B, "MAX_RUNS", 5):
            for i in range(8):
                B.save_run({"kind": "cpu", "at": i})
            self.assertEqual([r["at"] for r in B.history()], [7, 6, 5, 4, 3])


@unittest.skipUnless(os.name == "nt", "Windows only")
class DiskTempFile(unittest.TestCase):
    """The disk test writes a real (small) file in a private temp folder: it must never be left behind."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shb_disk_")
        self.env = mock.patch.dict(os.environ, {"TEMP": self.dir})
        self.env.start()
        self.drive = self.dir[:2].upper()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def left(self):
        return [f for f in os.listdir(self.dir) if f.startswith(B.TMP_PREFIX)]

    def test_short_run_cleans_up(self):
        r = B.disk(QuietJob(), self.drive, 64, 0.05)
        self.assertEqual(self.left(), [])
        self.assertEqual(set(r["tests"]), {"seq_q8", "seq_q1", "rnd_q32", "rnd_q1"})
        self.assertGreater(r["tests"]["seq_q8"]["read"], 0)
        self.assertGreater(r["score"], 0)

    def test_cancel_cleans_up(self):
        with self.assertRaises(Cancelled):
            B.disk(QuietJob(cancel_after=2), self.drive, 64, 0.05)
        self.assertEqual(self.left(), [])

    def test_error_cleans_up(self):
        with mock.patch.object(B, "_qd_run", side_effect=OSError(5, "boom")):
            with self.assertRaises(OSError):
                B.disk(QuietJob(), self.drive, 64, 0.05)
        self.assertEqual(self.left(), [])

    def test_leftovers_are_swept(self):
        stale = os.path.join(self.dir, B.TMP_PREFIX + "123-456.tmp")
        open(stale, "wb").close()
        B._sweep(self.dir)
        self.assertFalse(os.path.exists(stale))

    def test_rejects_odd_sizes(self):
        with self.assertRaises(ValueError):
            B.disk(QuietJob(), self.drive, 100, 0.05)


if __name__ == "__main__":
    unittest.main()

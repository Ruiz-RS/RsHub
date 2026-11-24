"""The cleanup scan reads the disk while the slow "which program has this open" query runs elsewhere."""
import os
import tempfile
import time
import unittest

from rshub.engine.removal import cleaner


class LazyInUse(unittest.TestCase):
    def test_temp_entries_wait_for_the_in_use_answer_only_after_reading(self):
        with tempfile.TemporaryDirectory() as root:
            for n in ("a", "b"):
                os.makedirs(os.path.join(root, n))
                with open(os.path.join(root, n, "f.tmp"), "wb") as f:
                    f.write(b"x" * 10)
            old = time.time() - 40 * 86400
            for n in ("a", "b"):
                os.utime(os.path.join(root, n, "f.tmp"), (old, old))
                os.utime(os.path.join(root, n), (old, old))
            order = []

            def late():
                order.append("asked")
                return {cleaner._norm(os.path.join(root, "b")): "demo.exe"}

            ticks = []
            rows = cleaner.temp_entries(root, 7, (), late, tick=lambda i, n: ticks.append((i, n)))
            by = {r["name"]: r for r in rows}
            self.assertEqual(by["a"]["status"], "clean")
            self.assertEqual(by["b"]["status"], "in_use")
            self.assertEqual(order, ["asked"])          # asked once, after the walk
            self.assertEqual(ticks[-1], (2, 2))
            same = cleaner.temp_entries(root, 7, (), {cleaner._norm(os.path.join(root, "b")): "demo.exe"})
            self.assertEqual([(r["name"], r["status"], r["size"]) for r in rows],
                             [(r["name"], r["status"], r["size"]) for r in same])

    def test_in_use_isolated_sees_this_process(self):
        got = cleaner.in_use_isolated(2.0)
        self.assertIn(cleaner._norm(os.getcwd()), got)


if __name__ == "__main__":
    unittest.main()

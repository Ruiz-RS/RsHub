"""Windows 8.3 short names (C:\\Users\\JOHNSM~1, often in %TEMP% for long or spaced user names) must compare equal to
the long names, or the safety rules treat a user's own files as another user's (and the other way around)."""
import ctypes
import os
import shutil
import tempfile
import unittest

from rshub.engine.removal import paths as rp


def short(p):
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024)
    return buf.value if 0 < n <= 1024 else p


class ShortNames(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.long = os.path.join(self.root, "A folder with a long name")
        os.makedirs(os.path.join(self.long, "inside"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_short_and_long_names_are_the_same_folder(self):
        s = short(self.long)
        if "~" not in s:
            self.skipTest("8.3 names are turned off on this drive")
        self.assertEqual(rp.norm_path(s), rp.norm_path(self.long))
        self.assertTrue(rp.is_within(os.path.join(s, "inside"), self.long))
        self.assertTrue(rp.is_within(os.path.join(self.long, "inside"), s))

    def test_a_missing_tail_keeps_the_expanded_head(self):
        s = short(self.long)
        if "~" not in s:
            self.skipTest("8.3 names are turned off on this drive")
        self.assertEqual(rp.norm_path(os.path.join(s, "not there", "file.txt")),
                         rp.norm_path(os.path.join(self.long, "not there", "file.txt")))

    def test_paths_without_short_names_are_unchanged(self):
        self.assertEqual(rp.norm_path(r"C:\Some\Path\\"), r"c:\some\path")
        self.assertEqual(rp.norm_path(r"C:\does-not-exist\x~1"), r"c:\does-not-exist\x~1")


if __name__ == "__main__":
    unittest.main()

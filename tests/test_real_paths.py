"""The safety rules compare the paths Windows really means, not the text they were written with: a junction or a
symlink into a protected folder, an 8.3 short name, other case, trailing dots or separators and a "..\\" cannot make a
protected folder look unprotected, and an ordinary folder is not blocked for resembling a protected one.

Everything is built in temporary folders (the "protected" folders are patched known folders inside them); the
checks on the real C:\\Windows only ask questions, nothing is touched.
"""
import _winapi
import ctypes
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from rshub.engine import storage
from rshub.engine.removal import paths, safety


def junction(target, link):
    _winapi.CreateJunction(target, link)


def short(p):
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024)
    return buf.value if 0 < n <= 1024 else p


def blocked(p):
    return safety.Context().check_path(p)[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sh_real_")
        self.profile = os.path.join(self.tmp, "Users", "bob")
        self.docs = os.path.join(self.profile, "Documents With A Long Name")
        self.ordinary = os.path.join(self.tmp, "Ordinary folder")
        for d in (self.docs, os.path.join(self.docs, "inside"), self.ordinary, os.path.join(self.profile, "AppData", "Local")):
            os.makedirs(d)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        kf = dict(paths.known_folders())
        kf.update(Profile=self.profile, Documents=self.docs, LocalAppData=os.path.join(self.profile, "AppData", "Local"))
        p = mock.patch.object(paths, "known_folders", return_value=kf)
        p.start()
        self.addCleanup(p.stop)
        self.links = []

    def link(self, target, name):
        link = os.path.join(self.tmp, name)
        junction(target, link)
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        return link


class JunctionsAndSymlinks(Base):
    def test_a_junction_into_a_protected_folder_is_protected(self):
        link = self.link(self.docs, "innocent-looking")
        self.assertIsNotNone(blocked(link))
        # what is inside a protected folder is judged as what it is, whichever way it is reached: the user's own
        # files (personal), even when the folder does not exist yet
        for tail in (("inside",), ("inside", "not", "there", "yet.txt")):
            direct = safety.Context().check_path(os.path.join(self.docs, *tail))
            via = safety.Context().check_path(os.path.join(link, *tail))
            self.assertEqual(via, direct)
            self.assertEqual(via, (None, True))

    def test_the_storage_rules_see_through_it_too(self):
        link = self.link(self.docs, "innocent-looking")
        r = storage.can_delete(link)
        self.assertFalse(r["ok"])
        self.assertTrue(r["why"])
        for tail in (("inside",), ("new", "file.bin")):
            self.assertEqual(storage.can_delete(os.path.join(link, *tail)), storage.can_delete(os.path.join(self.docs, *tail)))

    def test_a_junction_to_a_forbidden_tree_is_refused(self):
        win = paths.known_folders()["Windows"]
        link = self.link(win, "winlink")
        self.assertIsNotNone(blocked(link))
        self.assertIsNotNone(blocked(os.path.join(link, "System32", "drivers")))
        self.assertFalse(storage.can_delete(os.path.join(link, "System32"))["ok"])

    def test_a_redirected_protected_folder_is_protected_where_it_really_is(self):
        """Documents moved by the user (a junction to another drive or folder): the real folder is the protected one."""
        real = os.path.join(self.tmp, "Elsewhere", "My real documents")
        os.makedirs(os.path.join(real, "sub"))
        moved = os.path.join(self.profile, "Documents moved")
        junction(real, moved)
        self.addCleanup(lambda: os.path.isdir(moved) and os.rmdir(moved))
        kf = dict(paths.known_folders())
        kf["Documents"] = moved
        with mock.patch.object(paths, "known_folders", return_value=kf):
            self.assertIsNotNone(blocked(real))
            self.assertEqual(safety.Context().check_path(os.path.join(real, "sub")), (None, True))
            self.assertIsNotNone(blocked(moved))
            self.assertFalse(storage.can_delete(real)["ok"])

    def test_a_junction_to_an_ordinary_folder_is_still_ordinary(self):
        link = self.link(self.ordinary, "ordinary-link")
        self.assertIsNone(blocked(link))
        self.assertIsNone(blocked(os.path.join(link, "child")))

    def test_forms_show_both_names(self):
        link = self.link(self.docs, "innocent-looking")
        f = paths.forms(os.path.join(link, "inside"))
        self.assertEqual(len(f), 2)
        self.assertEqual(f[0], paths.norm_path(os.path.join(link, "inside")))
        self.assertEqual(f[1], paths.norm_path(os.path.join(self.docs, "inside")))
        self.assertTrue(paths.is_within_real(os.path.join(link, "inside"), self.docs))
        self.assertFalse(paths.is_within(os.path.join(link, "inside"), self.docs))   # the text comparison alone is fooled


class ShortNames(Base):
    def test_a_short_name_alias_of_a_protected_folder_is_protected(self):
        alias = short(self.docs)
        if "~" not in alias:
            self.skipTest("8.3 names are turned off on this drive")
        self.assertNotEqual(paths.norm_path(alias).lower(), "")
        self.assertIsNotNone(blocked(alias))
        self.assertEqual(safety.Context().check_path(os.path.join(alias, "inside")), (None, True))
        self.assertEqual(safety.Context().check_path(os.path.join(alias, "inside", "missing.txt")), (None, True))
        self.assertFalse(storage.can_delete(alias)["ok"])

    def test_real_program_files_alias(self):
        pf = paths.known_folders()["ProgramFiles"]
        alias = short(pf)
        if "~" not in alias:
            self.skipTest("8.3 names are turned off for Program Files")
        self.assertIsNotNone(blocked(alias))
        self.assertIsNotNone(blocked(os.path.join(alias, "Common Files")))
        self.assertFalse(storage.can_delete(alias)["ok"])

    def test_real_windows_alias_and_spellings(self):
        win = paths.known_folders()["Windows"]
        alias = short(win)
        for p in ([alias] if "~" in alias else []):
            self.assertIsNotNone(blocked(p))


class Spellings(unittest.TestCase):
    """C:\\Windows written in every way Windows accepts: protected under all of them, using the real system folders."""

    def test_windows_every_spelling(self):
        win = paths.known_folders()["Windows"]
        drive, rest = os.path.splitdrive(win)
        spellings = [win + ".", win + "..", win + " ", win + "\\", win + "\\\\", win.upper(), win.lower(), win.swapcase(),
                     win + "\\.", win + "\\System32\\..", win + "\\System32\\..\\", "\\\\?\\" + win,
                     win + "\\System32.", os.path.join(win, "SYSTEM32") + "\\"]
        for p in spellings:
            self.assertIsNotNone(blocked(p), p)
            self.assertFalse(storage.can_delete(p)["ok"], p)

    def test_program_files_and_users_every_spelling(self):
        k = paths.known_folders()
        for base in (k["ProgramFiles"], k["ProgramData"], k["Profile"]):
            for p in (base + ".", base.upper() + "\\", base.lower() + "\\\\", base + "\\."):
                self.assertIsNotNone(blocked(p), p)
                self.assertFalse(storage.can_delete(p)["ok"], p)

    def test_an_ordinary_folder_is_not_taken_for_a_protected_one(self):
        """Similar names are not the same folder: C:\\Windows2, C:\\Windows.old-style prefixes, a Windows folder inside temp."""
        tmp = tempfile.mkdtemp(prefix="sh_real_")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "Windows", "System32"))   # a folder NAMED Windows, somewhere ordinary
        for p in (os.path.join(tmp, "Windows"), os.path.join(tmp, "Windows", "System32")):
            self.assertIsNone(blocked(p), p)
            self.assertIsNone(storage._protected(p), p)
        drive = os.path.splitdrive(paths.known_folders()["Windows"])[0]
        for p in (drive + "\\Windows2\\x", drive + "\\WindowsApps-not\\x", drive + "\\Program Files Extra\\Foo"):
            self.assertIsNone(blocked(p), p)
            self.assertIsNone(storage._protected(p), p)


class Normalization(unittest.TestCase):
    def test_text_normalization_without_the_disk(self):
        self.assertEqual(paths.norm_path("C:\\Windows."), "c:\\windows")
        self.assertEqual(paths.norm_path("C:\\Windows \\"), "c:\\windows")
        self.assertEqual(paths.norm_path("C:\\Windows\\\\"), "c:\\windows")
        self.assertEqual(paths.norm_path("C:\\A\\..\\Windows\\System32\\.."), "c:\\windows")
        self.assertEqual(paths.norm_path("C:\\does-not-exist.\\a."), "c:\\does-not-exist\\a")
        self.assertEqual(paths.norm_path("\\\\?\\C:\\Windows"), "c:\\windows")
        self.assertEqual(paths.norm_path("\\??\\C:\\Windows"), "c:\\windows")
        self.assertEqual(paths.norm_path("C:\\"), "c:\\")

    def test_real_norm_resolves_what_exists_and_appends_the_rest(self):
        tmp = tempfile.mkdtemp(prefix="sh_real_")
        self.addCleanup(shutil.rmtree, tmp, True)
        real = os.path.join(tmp, "Real Folder")
        os.makedirs(real)
        link = os.path.join(tmp, "L")
        junction(real, link)
        self.addCleanup(os.rmdir, link)
        self.assertEqual(paths.real_norm(os.path.join(link, "a", "b.txt")), paths.norm_path(os.path.join(real, "a", "b.txt")))
        self.assertEqual(paths.real_norm(link.upper()), paths.norm_path(real))
        self.assertEqual(paths.real_norm(os.path.join(tmp, "missing.", "x")), paths.norm_path(os.path.join(tmp, "missing", "x")))

    def test_an_unreachable_or_odd_path_never_raises(self):
        for p in ("", None, "Q:\\no\\such\\drive", "C:\\bad|name\\x", "relative\\path", "\\\\no-such-server\\share\\x"):
            paths.forms(p)
            paths.real_norm(p)


class Speed(Base):
    def test_a_check_is_fast_and_the_protected_lists_are_cached(self):
        c = safety.Context()
        t = time.perf_counter()
        for _ in range(300):
            c.check_path(os.path.join(self.ordinary, "x", "y.txt"))
        per = (time.perf_counter() - t) / 300
        self.assertLess(per, 0.01, f"{per * 1000:.2f} ms per check")
        t = time.perf_counter()
        for _ in range(200):
            safety.protected_exact()
        self.assertLess((time.perf_counter() - t) / 200, 0.005)


if __name__ == "__main__":
    unittest.main()

"""RsHub's own updates (engine/selfupdate.py). Nothing is installed: a local web server plays GitHub."""
import functools
import hashlib
import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

os.environ["RSHUB_NO_DIALOGS"] = "1"  # (the updater also shows a message box on failure)
os.environ["RSHUB_NO_SPLASH"] = "1"  # the updater shows the start screen; a test must not

from rshub import ed25519
from rshub.engine import selfupdate as su


def setUpModule():  # the updater sets the start screen's language: the rest of the suite keeps its own
    from rshub import i18n
    setUpModule.saved = (i18n._lang, i18n._dict)


def tearDownModule():
    from rshub import i18n
    i18n._lang, i18n._dict = setUpModule.saved


SEED = bytes(range(32))  # a throwaway key for these tests; the real one never leaves the owner's PC
PUB = ed25519.public_key(SEED).hex()
OTHER_SEED = bytes(range(1, 33))


def sign_file(name, ver, data, seed=SEED):
    return ed25519.sign(seed, su.manifest(name, ver, hashlib.sha256(data).hexdigest()))


def release(ver, base, draft=False, name=None, sig=True):
    name = name or f"RsHub-Setup-{ver}.exe"
    r = {"tag_name": "v" + ver, "draft": draft, "prerelease": False, "body": "### New\n- thing",
            "html_url": f"{base}/tag/v{ver}", "published_at": "2026-11-18T00:00:00Z",
            "assets": [{"name": name, "size": 11, "browser_download_url": f"{base}/{name}"},
                       {"name": name + ".sha256", "size": 90, "browser_download_url": f"{base}/{name}.sha256"}]}
    if sig:
        r["assets"].append({"name": name + ".sig", "size": 64, "browser_download_url": f"{base}/{name}.sig"})
    return r


class Versions(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(su.newer("0.5.0", "0.4.0"))
        self.assertTrue(su.newer("v0.10.0", "0.9.9"))
        self.assertTrue(su.newer("1.0", "0.99.99"))
        self.assertFalse(su.newer("0.4.0", "0.4.0"))
        self.assertFalse(su.newer("0.3.9", "0.4.0"))
        self.assertFalse(su.newer("garbage", "0.4.0"))

    def test_pick_needs_the_installer_and_its_signature(self):
        base = "https://github.com/x/y/releases/download/v0.5.0"
        info = su.pick(release("0.5.0", base))
        self.assertEqual(info["ver"], "0.5.0")
        self.assertTrue(info["setup"]["url"].endswith("RsHub-Setup-0.5.0.exe"))
        rel = release("0.5.0", base)
        rel["assets"] = [a for a in rel["assets"] if not a["name"].endswith(".sha256")]  # the .sha256 is not needed
        self.assertIsNotNone(su.pick(rel))
        self.assertIsNone(su.pick(release("0.5.0", base, sig=False)))  # no .sig: never automatic
        rel = release("0.5.0", base)
        rel["assets"] = [a for a in rel["assets"] if a["name"] != "RsHub-Setup-0.5.0.exe.sig"]
        rel["assets"].append({"name": "RsHub-Setup-0.4.0.exe.sig", "size": 64, "browser_download_url": base + "/x"})
        self.assertIsNone(su.pick(rel))  # a .sig of another file does not count
        self.assertIsNone(su.pick(release("0.5.0", base, draft=True)))
        # an installer for another version than the tag is not taken
        self.assertIsNone(su.pick(release("0.5.0", base, name="RsHub-Setup-0.4.0.exe")))

    def test_pick_refuses_plain_http(self):
        with mock.patch.dict(os.environ, {"RSHUB_UPDATE_URL": ""}):
            self.assertIsNone(su.pick(release("0.5.0", "http://example.com")))


class Serve:
    def __init__(self, files):
        self.dir = tempfile.mkdtemp()
        for n, data in files.items():
            with open(os.path.join(self.dir, n), "wb") as fh:
                fh.write(data)

        class Q(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Q, directory=self.dir))
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)


class Manifest(unittest.TestCase):
    """A signature is tied to the file name, the version and the exact bytes, and to a key the app trusts."""

    def setUp(self):
        self.data = b"installer bytes"
        self.name = "RsHub-Setup-2.1.0.exe"
        self.sig = sign_file(self.name, "2.1.0", self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()

    def ok(self, name=None, ver="2.1.0", sha=None, sig=None, keys=None):
        return su.signature_ok(name or self.name, ver, sha or self.sha, sig or self.sig, [PUB] if keys is None else keys)

    def test_manifest_text(self):
        self.assertEqual(su.manifest("a.exe", "2.1.0", "AB"), b"RsHub release\nfile: a.exe\nversion: 2.1.0\nsha256: ab\n")

    def test_valid(self):
        self.assertTrue(self.ok())

    def test_wrong_version_name_or_hash_is_refused(self):
        self.assertFalse(self.ok(ver="1.0"))  # an old signed file passed off as a newer version
        self.assertFalse(self.ok(name="RsHub-Setup-1.0.exe"))
        self.assertFalse(self.ok(sha=hashlib.sha256(b"tampered").hexdigest()))

    def test_unknown_key_or_no_keys_is_refused(self):
        self.assertFalse(self.ok(sig=sign_file(self.name, "2.1.0", self.data, OTHER_SEED)))
        self.assertFalse(self.ok(keys=[]))
        self.assertFalse(self.ok(keys=["not hex", "00" * 32]))

    def test_any_trusted_key_counts_so_keys_can_rotate(self):
        new_pub = ed25519.public_key(OTHER_SEED).hex()
        self.assertTrue(self.ok(keys=[new_pub, PUB]))
        self.assertTrue(self.ok(sig=sign_file(self.name, "2.1.0", self.data, OTHER_SEED), keys=[PUB, new_pub]))

    def test_garbage_signature_is_refused(self):
        self.assertFalse(self.ok(sig=b"x"))
        self.assertFalse(self.ok(sig=b"x" * 64))
        self.assertFalse(self.ok(sig=self.sig.hex().encode()))

    def test_the_test_key_is_not_embedded_in_the_app(self):
        self.assertNotIn(PUB, su.TRUSTED_KEYS)


class ServedUpdates(unittest.TestCase):
    """A local web server plays GitHub (serve), and the updater runs against it (run_it)."""
    NAME = "RsHub-Setup-9.9.9.exe"

    def setUp(self):
        self.payload = b"fake-setup!"
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def serve(self, sig="good", payload=None):
        """sig: "good" (signed by the trusted key), None (no .sig in the release), or the bytes served as .sig."""
        payload = self.payload if payload is None else payload
        files = {self.NAME: payload, self.NAME + ".sha256": f"{hashlib.sha256(payload).hexdigest()}  {self.NAME}\n".encode()}
        if sig == "good":
            sig = sign_file(self.NAME, "9.9.9", self.payload)
        if sig is not None:
            files[self.NAME + ".sig"] = sig
        s = Serve(files)
        with open(os.path.join(s.dir, "latest.json"), "w", encoding="utf-8") as fh:
            json.dump(release("9.9.9", s.base, sig=sig is not None), fh)
        self.addCleanup(s.close)
        for p in (mock.patch.dict(os.environ, {"RSHUB_UPDATE_URL": s.base + "/latest.json",
                                               "RSHUB_UPDATE_DIR": self.tmp, "LOCALAPPDATA": self.tmp}),
                  mock.patch.object(su, "TRUSTED_KEYS", [PUB]), mock.patch.object(su, "FROZEN", True),
                  mock.patch.object(su, "per_machine", lambda: False), mock.patch.object(su, "_app_running", lambda: False)):
            p.start()
            self.addCleanup(p.stop)
        return s

    def run_it(self):
        with mock.patch.object(su.subprocess, "Popen") as popen:
            code = su.run_updater()
        return code, su.state(), popen


class CheckAndDownload(ServedUpdates):
    def test_check_reports_a_newer_version(self):
        self.serve()
        r = su.check()
        self.assertTrue(r["ok"])
        self.assertTrue(r["newer"])
        self.assertEqual(r["latest"], "9.9.9")
        self.assertIn("thing", r["notes"])
        self.assertNotEqual(r.get("mode"), "page")
        self.assertFalse(r.get("unsigned"))

    def test_check_offers_only_the_page_for_an_unsigned_release(self):
        self.serve(sig=None)
        r = su.check()
        self.assertTrue(r["ok"] and r["newer"])
        self.assertEqual(r["mode"], "page")
        self.assertTrue(r["unsigned"])
        self.assertEqual(r["latest"], "9.9.9")

    def test_check_offline_is_not_an_exception(self):
        with mock.patch.dict(os.environ, {"RSHUB_UPDATE_URL": "http://127.0.0.1:9/none.json"}):
            r = su.check()
        self.assertFalse(r["ok"])

    def assert_refused(self, code, st, popen):
        self.assertNotEqual(code, 0)
        self.assertEqual(st["st"], "failed")
        self.assertEqual(st["why"], "signature")
        popen.assert_not_called()
        self.assertFalse(any(f.endswith((".exe", ".part")) for f in os.listdir(su.update_dir())))

    def test_updater_verifies_and_hands_over_to_the_installer(self):
        self.serve()
        with mock.patch.object(su, "_admin", return_value=False):  # CI runners are elevated
            code, st, popen = self.run_it()
        self.assertEqual(code, 0)
        self.assertEqual(st["st"], "installing")
        args = popen.call_args[0][0]
        self.assertTrue(args[0].endswith(self.NAME))
        self.assertIn("/SILENT", args)  # Setup shows its own progress window
        self.assertNotIn("/VERYSILENT", args)
        self.assertIn("/CURRENTUSER", args)  # installed for one user: the update too
        self.assertTrue(any(a.startswith("/SELFUPDATE=") for a in args))
        self.assertNotIn("/NOREOPEN=1", args)  # not elevated: Setup opens the app again itself, as the user

    def test_an_elevated_updater_leaves_the_reopening_to_the_user(self):
        """From an elevated updater, Setup's "open RsHub" would run elevated (or as the administrator who
        answered the prompt): it is told not to, and the reopener the app left (as the user) opens it."""
        self.serve()
        with mock.patch.object(su, "_admin", return_value=True), mock.patch.object(su, "per_machine", return_value=False):
            code, st, popen = self.run_it()
        self.assertEqual(code, 0)
        self.assertIn("/NOREOPEN=1", popen.call_args[0][0])

    def test_an_unsigned_release_is_never_downloaded_or_run(self):
        self.serve(sig=None)  # no .sig asset: pick() gives nothing
        code, st, popen = self.run_it()
        self.assertEqual(st["st"], "uptodate")
        popen.assert_not_called()
        self.assertFalse(any(f.endswith(".exe") for f in os.listdir(su.update_dir())))

    def test_forged_release_with_matching_sha256_is_refused(self):
        """A stolen GitHub account: the thief uploads a new file, its .sha256 and a signature of his own key."""
        evil = b"malware!!!!"
        forged = ed25519.sign(OTHER_SEED, su.manifest(self.NAME, "9.9.9", hashlib.sha256(evil).hexdigest()))
        self.serve(sig=forged, payload=evil)
        self.assert_refused(*self.run_it())

    def test_valid_signature_of_another_file_is_refused(self):
        """The thief replays the author's signature (it is public) with a different file."""
        self.serve(payload=b"malware!!!!")
        self.assert_refused(*self.run_it())

    def test_signature_for_another_version_is_refused(self):
        self.serve(sig=sign_file(self.NAME, "9.9.8", self.payload))
        self.assert_refused(*self.run_it())

    def test_garbage_signature_is_refused(self):
        self.serve(sig=b"nope")
        self.assert_refused(*self.run_it())

    def test_background_download_stops_at_staged_and_is_reused(self):
        """New features download in the background and wait for the next start; the start reuses the file."""
        s = self.serve()
        os.makedirs(os.path.dirname(su._prefetch_flag()), exist_ok=True)
        open(su._prefetch_flag(), "w").close()
        code, st, popen = self.run_it()
        self.assertEqual((code, st["st"]), (0, "staged"))
        self.assertEqual(su.staged(), "9.9.9")
        self.assertFalse(os.path.exists(su._prefetch_flag()))
        popen.assert_not_called()
        exe = os.path.join(su.update_dir(), self.NAME)
        mtime = os.path.getmtime(exe)
        # the next start: no flag, the same file (its signature checked again) goes to the installer
        os.remove(os.path.join(s.dir, self.NAME))  # proves nothing is downloaded again
        code, st, popen = self.run_it()
        self.assertEqual((code, st["st"]), (0, "installing"))
        self.assertEqual(os.path.getmtime(exe), mtime)
        self.assertTrue(popen.call_args[0][0][0].endswith(self.NAME))

    def test_a_tampered_staged_file_is_downloaded_again(self):
        self.serve()
        os.makedirs(su.update_dir(), exist_ok=True)
        exe = os.path.join(su.update_dir(), self.NAME)
        with open(exe, "wb") as fh:
            fh.write(b"something else")
        self.assertEqual(self.run_it()[0], 0)
        with open(exe, "rb") as fh:
            self.assertEqual(fh.read(), self.payload)

    def test_a_staged_file_swapped_for_a_forged_one_is_never_run(self):
        """Staged, then the file is replaced and the server serves the same forgery: refused at start."""
        s = self.serve()
        os.makedirs(os.path.dirname(su._prefetch_flag()), exist_ok=True)
        open(su._prefetch_flag(), "w").close()
        self.assertEqual(self.run_it()[0], 0)
        exe = os.path.join(su.update_dir(), self.NAME)
        for path in (exe, os.path.join(s.dir, self.NAME)):
            with open(path, "wb") as fh:
                fh.write(b"malware!!!!")
        self.assert_refused(*self.run_it())

    def test_updater_does_nothing_when_up_to_date(self):
        s = self.serve()
        with open(os.path.join(s.dir, "latest.json"), "w", encoding="utf-8") as fh:
            json.dump(release(su.VERSION, s.base), fh)
        code, st, popen = self.run_it()
        self.assertEqual((code, st["st"]), (0, "uptodate"))
        popen.assert_not_called()


class AfterUpdate(unittest.TestCase):
    def test_note_is_read_once(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"APPDATA": tmp}), mock.patch.object(su, "spawn_reopener"):
            su.finish(su.VERSION, "privacy")
            n = su.after_update()
            self.assertTrue(n["ok"])
            self.assertEqual(n["route"], "privacy")
            self.assertIsNone(su.after_update())


if __name__ == "__main__":
    unittest.main()


class Portable(unittest.TestCase):
    """The portable version updates with the .zip of the release and replaces its own files."""

    def test_pick_takes_the_zip_in_the_portable_version(self):
        base = "https://github.com/x/y/releases/download/v0.6.0"
        rel = release("0.6.0", base)
        rel["assets"] += [{"name": "RsHub-Portable-0.6.0.zip", "size": 9, "browser_download_url": base + "/RsHub-Portable-0.6.0.zip"},
                          {"name": "RsHub-Portable-0.6.0.zip.sha256", "size": 90, "browser_download_url": base + "/RsHub-Portable-0.6.0.zip.sha256"},
                          {"name": "RsHub-Portable-0.6.0.zip.sig", "size": 64, "browser_download_url": base + "/RsHub-Portable-0.6.0.zip.sig"}]
        self.assertTrue(su.pick(rel, portable=True)["setup"]["url"].endswith(".zip"))
        self.assertTrue(su.pick(rel, portable=False)["setup"]["url"].endswith(".exe"))
        self.assertIsNone(su.pick(release("0.6.0", base), portable=True))  # an old release without the .zip
        rel["assets"] = [x for x in rel["assets"] if x["name"] != "RsHub-Portable-0.6.0.zip.sig"]
        self.assertIsNone(su.pick(rel, portable=True))  # the .zip has no signature
        self.assertIsNotNone(su.pick(rel, portable=False))  # (the installer's is there)

    def test_unpack_refuses_paths_outside_and_needs_the_exe(self):
        import zipfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        good = os.path.join(tmp, "good.zip")
        with zipfile.ZipFile(good, "w") as z:
            z.writestr("RsHub/RsHub.exe", b"x")
            z.writestr("RsHub/_internal/a.dll", b"y")
        base = su.unpack_portable(good, os.path.join(tmp, "out"))
        self.assertTrue(os.path.isfile(os.path.join(base, "RsHub.exe")))
        evil = os.path.join(tmp, "evil.zip")
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../escape.txt", b"no")
            z.writestr("RsHub.exe", b"x")
        with self.assertRaises(ValueError):
            su.unpack_portable(evil, os.path.join(tmp, "out2"))
        self.assertFalse(os.path.exists(os.path.join(tmp, "escape.txt")))
        empty = os.path.join(tmp, "empty.zip")
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("readme.txt", b"x")
        with self.assertRaises(ValueError):
            su.unpack_portable(empty, os.path.join(tmp, "out3"))


class PortableUpdater(unittest.TestCase):
    """The whole updater for the portable copy: the signed .zip is unpacked, a forged one is not."""
    NAME = "RsHub-Portable-9.9.9.zip"

    def zip_bytes(self, marker):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("RsHub/RsHub.exe", marker)
        return buf.getvalue()

    def run_with(self, payload, sig):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        files = {self.NAME: payload, self.NAME + ".sig": sig}
        s = Serve(files)
        self.addCleanup(s.close)
        rel = release("9.9.9", s.base, name=self.NAME)
        with open(os.path.join(s.dir, "latest.json"), "w", encoding="utf-8") as fh:
            json.dump(rel, fh)
        with mock.patch.dict(os.environ, {"RSHUB_UPDATE_URL": s.base + "/latest.json", "RSHUB_UPDATE_DIR": tmp,
                                          "LOCALAPPDATA": tmp}),                 mock.patch.object(su, "TRUSTED_KEYS", [PUB]), mock.patch.object(su, "FROZEN", True),                 mock.patch.object(su, "_portable", lambda: True), mock.patch.object(su, "per_machine", lambda: False):
            code = su.run_updater()
            return code, su.state(), tmp

    def test_signed_zip_is_unpacked(self):
        good = self.zip_bytes(b"new exe")
        code, st, tmp = self.run_with(good, sign_file(self.NAME, "9.9.9", good))
        self.assertEqual((code, st["st"]), (0, "ready"))
        self.assertTrue(os.path.isfile(os.path.join(st["new"], "RsHub.exe")))
        self.assertFalse(os.path.exists(os.path.join(tmp, "update", self.NAME)) or os.path.exists(os.path.join(tmp, self.NAME)))

    def test_forged_zip_is_refused_and_deleted(self):
        good, evil = self.zip_bytes(b"new exe"), self.zip_bytes(b"malware")
        code, st, tmp = self.run_with(evil, sign_file(self.NAME, "9.9.9", good))  # the real signature, another file
        self.assertEqual((st["st"], st["why"]), ("failed", "signature"))
        self.assertNotEqual(code, 0)
        left = [f for r, _, fs in os.walk(tmp) for f in fs if f.endswith((".zip", ".part"))]
        self.assertEqual(left, [])
        self.assertFalse(any(os.path.isdir(os.path.join(r, "new")) for r, ds, _ in os.walk(tmp)))


class PortablePaths(unittest.TestCase):
    def test_data_next_to_the_program(self):
        from rshub import paths
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        d = os.path.join(tmp, "RsHub", "Data")
        with mock.patch.object(paths, "PORTABLE", True), mock.patch.object(paths, "_portable_data", None), \
                mock.patch.dict(os.environ, {"RSHUB_PORTABLE_DATA": d}):
            self.assertEqual(paths.data_dir(), d)
            self.assertEqual(paths.settings_file(), os.path.join(d, "settings.json"))
            self.assertTrue(paths.local_dir().startswith(d))

    def test_read_only_drive_falls_back_to_this_pc(self):
        from rshub import paths
        with mock.patch.object(paths, "PORTABLE", True), mock.patch.object(paths, "_portable_data", None), \
                mock.patch.dict(os.environ, {"RSHUB_PORTABLE_DATA": r"Q:\nowhere\Data"}):
            d = paths.data_dir()
        self.assertIn("Portable-", d)
        self.assertTrue(d.lower().startswith(os.environ["LOCALAPPDATA"].lower()))


class PortableFinish(unittest.TestCase):
    """The new portable copy (in the update folder) copies itself over the old one and keeps the Data folder."""

    def test_new_copy_replaces_program_files_and_keeps_data(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        dst = os.path.join(root, "RsHub")
        src = os.path.join(dst, "update", "new", "RsHub")
        for d in (os.path.join(dst, "_internal"), os.path.join(dst, "Data"), os.path.join(src, "_internal")):
            os.makedirs(d)
        files = {os.path.join(dst, "RsHub.exe"): "old exe", os.path.join(dst, "_internal", "old.dll"): "old",
                 os.path.join(dst, "Data", "settings.json"): "{}", os.path.join(src, "RsHub.exe"): "new exe",
                 os.path.join(src, "_internal", "new.dll"): "new", os.path.join(src, "RsHub.portable"): "m"}
        for f, c in files.items():
            with open(f, "w") as fh:
                fh.write(c)
        with mock.patch.object(su, "install_dir", lambda: src), mock.patch.object(su.subprocess, "Popen") as popen:
            self.assertEqual(su.finish_update(dst, "999999"), 0)
        self.assertEqual(open(os.path.join(dst, "RsHub.exe")).read(), "new exe")
        self.assertEqual(os.listdir(os.path.join(dst, "_internal")), ["new.dll"])
        self.assertEqual(open(os.path.join(dst, "Data", "settings.json")).read(), "{}")
        self.assertFalse([n for n in os.listdir(dst) if n.endswith(".old")])
        with open(os.path.join(dst, "update", "state.json")) as fh:
            self.assertEqual(json.load(fh)["st"], "done")
        self.assertEqual(popen.call_args[0][0], [os.path.join(os.path.realpath(dst), "RsHub.exe")])

    def test_refuses_a_copy_that_is_not_inside_the_update_folder(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        dst = os.path.join(root, "RsHub")
        os.makedirs(dst)
        open(os.path.join(dst, "RsHub.exe"), "w").close()
        with mock.patch.object(su, "install_dir", lambda: os.path.join(root, "elsewhere")), mock.patch.object(su.subprocess, "Popen") as popen:
            self.assertEqual(su.finish_update(dst, "1"), 2)
        # nothing is copied; the old program (which closed for the update) opens again as it was (by its real
        # path: where the temp folder is a short 8.3 name, like CI's RUNNER~1, the two spellings differ)
        self.assertEqual(popen.call_args[0][0], [os.path.join(os.path.realpath(dst), "RsHub.exe")])
        self.assertFalse([n for n in os.listdir(dst) if n.endswith(".old")])

    def test_a_copy_at_the_root_of_a_drive_can_update(self):
        """A portable copy in E:\\ : dst + os.sep was "E:\\\\" and never matched, so it could never update."""
        self.assertTrue(su._under("E:\\update\\new", "E:\\"))
        self.assertFalse(su._under("F:\\update\\new", "E:\\"))


class InstallModes(ServedUpdates):
    """Per-user installs update directly; an all-users install asks Windows once and then has its task."""

    def frozen(self, per_machine, admin, task, portable=False):
        for p in (mock.patch.object(su, "FROZEN", True), mock.patch.object(su, "_portable", lambda: portable),
                  mock.patch.object(su, "per_machine", lambda: per_machine), mock.patch.object(su, "_admin", lambda: admin),
                  mock.patch.object(su, "_writable", lambda d: not per_machine), mock.patch.object(su, "_task_exists", lambda: task)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_per_user_install_updates_directly_with_no_task_and_no_prompt(self):
        self.frozen(per_machine=False, admin=False, task=False)
        self.assertEqual(su.mode(), "direct")
        with mock.patch.object(su.subprocess, "Popen") as popen, mock.patch.object(su.subprocess, "run") as run, \
                mock.patch.object(su.ctypes.windll.shell32, "ShellExecuteW") as uac:
            r = su.start(allow_uac=False)
        self.assertEqual((r["ok"], r["mode"]), (True, "direct"))
        self.assertEqual(popen.call_args[0][0][1:], ["--self-update"])
        run.assert_not_called()  # no schtasks
        uac.assert_not_called()

    def test_an_all_users_install_modes(self):
        self.frozen(per_machine=True, admin=False, task=False)
        self.assertEqual(su.mode(), "uac")
        with mock.patch.object(su, "_task_exists", lambda: True):
            self.assertEqual(su.mode(), "task")
        with mock.patch.object(su, "_admin", lambda: True):
            self.assertEqual(su.mode(), "direct")

    def test_the_background_download_never_asks_windows(self):
        self.frozen(per_machine=True, admin=False, task=False)
        with mock.patch.object(su.ctypes.windll.shell32, "ShellExecuteW") as uac, mock.patch.object(su.subprocess, "Popen") as popen:
            self.assertFalse(su.prefetch()["ok"])  # a new-features version shows the update window instead
            self.assertFalse(os.path.exists(su._prefetch_flag()))
            self.assertFalse(su.start(allow_uac=False)["ok"])
        uac.assert_not_called()
        popen.assert_not_called()

    def test_a_click_on_update_asks_windows_once(self):
        self.frozen(per_machine=True, admin=False, task=False)
        with mock.patch.object(su.ctypes.windll.shell32, "ShellExecuteW", return_value=42) as uac:
            r = su.start(allow_uac=True)
        self.assertEqual((r["ok"], r["mode"]), (True, "uac"))
        self.assertEqual(uac.call_args[0][1], "runas")
        self.assertEqual(uac.call_args[0][3], "--self-update")

    def test_the_staged_update_at_start_may_ask_windows(self):
        self.frozen(per_machine=True, admin=False, task=False)
        with mock.patch.object(su, "staged", lambda: "9.9.9"), mock.patch.object(su, "start", return_value={"ok": False}) as start:
            self.assertFalse(su.apply_staged_at_start())
        start.assert_called_once_with(allow_uac=True)

    def test_the_elevated_updater_of_an_all_users_install_makes_the_task_once(self):
        self.serve()
        self.frozen(per_machine=True, admin=True, task=False)
        with mock.patch.object(su, "ensure_task", return_value=True) as ensure:
            code, st, popen = self.run_it()
        self.assertEqual((code, st["st"]), (0, "installing"))
        ensure.assert_called_once_with()
        self.assertIn("/ALLUSERS", popen.call_args[0][0])

    def test_an_existing_task_is_left_alone_and_a_per_user_install_never_gets_one(self):
        self.serve()
        self.frozen(per_machine=True, admin=True, task=True)
        with mock.patch.object(su, "ensure_task") as ensure:
            self.run_it()
        ensure.assert_not_called()
        self.frozen(per_machine=False, admin=True, task=False)  # a per-user copy running as administrator
        with mock.patch.object(su, "ensure_task") as ensure:
            code, st, popen = self.run_it()
        ensure.assert_not_called()
        self.assertIn("/CURRENTUSER", popen.call_args[0][0])

    def test_a_task_that_cannot_be_made_does_not_stop_the_update(self):
        self.serve()
        self.frozen(per_machine=True, admin=True, task=False)
        with mock.patch.object(su, "ensure_task", side_effect=OSError("no")):
            code, st, popen = self.run_it()
        self.assertEqual((code, st["st"]), (0, "installing"))

    def test_the_uninstaller_only_removes_the_tasks_that_start_this_copy(self):
        from rshub import elevation, startup
        with mock.patch.object(elevation, "task_opens_this_copy", lambda name: False), \
                mock.patch.object(su, "_task_exists", lambda: False), mock.patch("subprocess.run") as run, \
                mock.patch.object(su, "delete_task") as delete, mock.patch.object(startup.autostart, "remove_if_ours"):
            startup.uninstall_cleanup()
        run.assert_not_called()  # another install's tasks (a per-user copy next to an all-users one) stay
        delete.assert_not_called()
        with mock.patch.object(elevation, "task_opens_this_copy", lambda name: name == elevation.ADMIN_TASK), \
                mock.patch.object(su, "_task_exists", lambda: True), mock.patch("subprocess.run") as run, \
                mock.patch.object(su, "delete_task") as delete, mock.patch.object(startup.autostart, "remove_if_ours"):
            startup.uninstall_cleanup()
        self.assertEqual([c[0][0][3] for c in run.call_args_list], [elevation.ADMIN_TASK])
        delete.assert_called_once_with()


class TaskPaths(unittest.TestCase):
    """The scheduled tasks are matched to this copy by the path in their XML, where Windows escapes &, < and >."""

    def query(self, command, args=""):
        xml = (f'<?xml version="1.0" encoding="UTF-16"?><Task><Actions><Exec><Command>{command}</Command>'
               f'<Arguments>{args}</Arguments></Exec></Actions></Task>')
        return mock.Mock(returncode=0, stdout=b"\xff\xfe" + xml.encode("utf-16-le"))

    def test_a_folder_with_an_ampersand_still_matches(self):
        from rshub import config, elevation
        exe = "D:\Tom & Jerry\RsHub\RsHub.exe"
        with mock.patch.object(su.sys, "executable", exe), \
                mock.patch.object(su.subprocess, "run", return_value=self.query("D:\Tom &amp; Jerry\RsHub\RsHub.exe")):
            self.assertTrue(su._task_exists())
        with mock.patch.object(su.subprocess, "run", return_value=self.query("&quot;D:\Tom &amp; Jerry\RsHub\RsHub.exe&quot;")), \
                mock.patch.object(config, "FROZEN", True), mock.patch.object(config, "launch_cmd", return_value=(exe, "--from-task")):
            self.assertTrue(elevation.task_opens_this_copy(elevation.ADMIN_TASK))
        with mock.patch.object(su.sys, "executable", exe), \
                mock.patch.object(su.subprocess, "run", return_value=self.query("D:\Other\RsHub.exe")):
            self.assertFalse(su._task_exists())


class ElevatedUpdaterTakesNoOrders(ServedUpdates):
    """An installed copy running elevated (the silent update task) ignores the test switches any program running as
    the user could set, and runs exactly the file it verified."""

    def elevated_frozen(self, on=True):
        from rshub import paths
        for p in (mock.patch.object(su.sys, "frozen", on, create=True), mock.patch.object(paths, "_elevated", lambda: on)):
            p.start()
            self.addCleanup(p.stop)

    def test_test_switches_are_ignored_when_installed_and_elevated(self):
        from rshub import paths
        env = {"RSHUB_UPDATE_DIR": self.tmp, "RSHUB_UPDATE_URL": "http://127.0.0.1:9/evil.json",
               "RSHUB_PORTABLE": "1", "RSHUB_PORTABLE_DATA": self.tmp}
        with mock.patch.dict(os.environ, env):
            self.elevated_frozen()
            self.assertNotEqual(os.path.normcase(su.update_dir()), os.path.normcase(self.tmp))
            self.assertEqual(su.update_dir(), os.path.join(su.install_dir(), "update"))
            for k in env:
                self.assertIsNone(paths.env(k), k)
            self.assertFalse(paths._portable())
            self.assertIsNone(su._pick(release("9.9.9", "http://127.0.0.1:9")))  # plain http is refused again
            with mock.patch.object(su, "_get", return_value=b"{}") as get:
                su._latest()
            self.assertEqual(get.call_args[0][0], su.API)

    def test_source_runs_and_tests_keep_the_switches(self):
        from rshub import paths
        with mock.patch.dict(os.environ, {"RSHUB_UPDATE_DIR": self.tmp}):
            self.elevated_frozen(False)
            with mock.patch.object(paths, "_elevated", lambda: True):  # elevated but from source
                self.assertEqual(su.update_dir(), self.tmp)
                self.assertEqual(paths.env("RSHUB_UPDATE_DIR"), self.tmp)

    def test_the_verified_installer_cannot_be_swapped_before_it_runs(self):
        self.serve()
        tries = {}

        def popen(args, **kw):
            exe = args[0]
            self.assertTrue(os.path.isabs(exe) and not exe.startswith("\\?\\"))
            for what, fn in (("write", lambda: open(exe, "r+b").close()), ("delete", lambda: os.remove(exe)),
                             ("rename", lambda: os.replace(exe, exe + ".x")),
                             ("folder", lambda: os.replace(su.update_dir(), su.update_dir() + ".x"))):
                try:
                    fn()
                    tries[what] = "allowed"
                except OSError:
                    tries[what] = "refused"
            return mock.Mock(pid=1, poll=lambda: 0)
        with mock.patch.object(su, "_admin", return_value=False), mock.patch.object(su.subprocess, "Popen", side_effect=popen):
            code = su.run_updater()
        self.assertEqual((code, su.state()["st"]), (0, "installing"))
        self.assertEqual(tries, {"write": "refused", "delete": "refused", "rename": "refused", "folder": "refused"})
        open(os.path.join(su.update_dir(), self.NAME), "r+b").close()  # released once Windows has started it

    def test_held_reads_the_bytes_it_locks(self):
        p = os.path.join(self.tmp, "a.exe")
        with open(p, "wb") as fh:
            fh.write(b"abc")
        h = su._held(p)
        try:
            self.assertEqual(h.sha256, hashlib.sha256(b"abc").hexdigest())
            self.assertEqual(os.path.normcase(os.path.realpath(h.path)), os.path.normcase(os.path.realpath(p)))
        finally:
            h.close()
        with self.assertRaises(OSError):
            su._held(os.path.join(self.tmp, "missing.exe"))

    def test_an_elevated_installer_unpacks_in_windows_temp(self):
        env = su._admin_temp_env()
        self.assertTrue(os.path.normcase(env["TEMP"]).endswith(os.path.normcase("\Temp")))
        self.assertEqual(env["TEMP"], env["TMP"])
        self.assertNotEqual(os.path.normcase(env["TEMP"]), os.path.normcase(os.environ.get("TEMP", "")))


class HelpersAndPortableData(unittest.TestCase):
    def test_a_waiting_updater_or_other_helper_is_not_the_open_app(self):
        import types
        exe = su.sys.executable

        def proc(pid, *args):
            return types.SimpleNamespace(info={"pid": pid, "name": os.path.basename(exe), "exe": exe,
                                               "cmdline": [exe, *args]})
        helpers = [proc(11, "--self-update"), proc(12, "--quit"), proc(13, "--warm"), proc(14, "--multiprocessing-fork")]
        with mock.patch("psutil.process_iter", return_value=helpers):
            self.assertFalse(su._app_running())
        with mock.patch("psutil.process_iter", return_value=helpers + [proc(15, "--tray")]):
            self.assertTrue(su._app_running())
        with mock.patch("psutil.process_iter", return_value=[types.SimpleNamespace(
                info={"pid": 16, "name": os.path.basename(exe), "exe": None, "cmdline": None})]):
            self.assertTrue(su._app_running())  # another account's copy (nothing readable) still counts

    def test_the_new_portable_copy_uses_the_old_copys_data(self):
        from rshub import paths
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        dst = os.path.join(root, "RsHub")
        new = os.path.join(dst, "update", "new", "RsHub")
        os.makedirs(new)
        os.makedirs(os.path.join(dst, "Data"))
        with mock.patch.object(paths, "PORTABLE", True), mock.patch.object(paths, "_portable_data", None), \
                mock.patch.object(paths, "program_dir", lambda: new), mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(paths.sys, "argv", ["RsHub.exe", "--finish-update", dst, "123"]):
            os.environ.pop("RSHUB_PORTABLE_DATA", None)
            self.assertEqual(paths.data_dir(), os.path.join(dst, "Data"))
        self.assertFalse(os.path.exists(os.path.join(new, "Data")))


class InstallNowDuringABackgroundDownload(ServedUpdates):
    """Through the update task, a click on Update while the background download runs only follows that run (Windows
    doesn't start a second copy of a running task): the update.now flag makes it install instead of stopping at
    "staged"."""

    def prefetching(self):
        os.makedirs(os.path.dirname(su._prefetch_flag()), exist_ok=True)
        open(su._prefetch_flag(), "w").close()

    def test_without_a_click_it_stays_staged(self):
        self.serve()
        self.prefetching()
        code, st, popen = self.run_it()
        self.assertEqual(st["st"], "staged")
        popen.assert_not_called()

    def test_a_click_meanwhile_installs_it_now(self):
        self.serve()
        self.prefetching()
        open(su._now_flag(), "w").close()
        with mock.patch.object(su, "_admin", return_value=False):
            code, st, popen = self.run_it()
        self.assertEqual((code, st["st"]), (0, "installing"))
        self.assertFalse(os.path.exists(su._now_flag()))

    def test_the_click_through_the_task_leaves_the_flag_and_the_background_one_does_not(self):
        for p in (mock.patch.object(su, "FROZEN", True), mock.patch.object(su, "_portable", lambda: False),
                  mock.patch.object(su, "per_machine", lambda: True), mock.patch.object(su, "_admin", lambda: False),
                  mock.patch.object(su, "_task_exists", lambda: True), mock.patch.object(su, "_updater_answered", lambda at: True),
                  mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp})):
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(su.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertTrue(su.prefetch()["ok"])
            self.assertFalse(os.path.exists(su._now_flag()))
            self.assertEqual(su.start(allow_uac=True)["mode"], "task")
            self.assertTrue(os.path.exists(su._now_flag()))
            su.prefetch()  # a later background download never inherits it
            self.assertFalse(os.path.exists(su._now_flag()))

    def test_a_run_that_installs_anyway_consumes_a_stale_flag(self):
        self.serve()
        os.makedirs(os.path.dirname(su._now_flag()), exist_ok=True)
        open(su._now_flag(), "w").close()
        with mock.patch.object(su, "_admin", return_value=False):
            code, st, popen = self.run_it()
        self.assertEqual(st["st"], "installing")
        self.assertFalse(os.path.exists(su._now_flag()))

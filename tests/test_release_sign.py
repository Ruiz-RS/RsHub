"""tools/release_sign.py with throwaway keys in temp folders (never the owner's real key)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import release_sign as rs  # noqa: E402

from rshub.engine import selfupdate as su  # noqa: E402


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rs.main(list(argv))
    return code, out.getvalue()


@unittest.skipUnless(sys.platform == "win32", "the key is protected with Windows DPAPI")
class ReleaseTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.keys = os.path.join(self.tmp, "keys")
        p = mock.patch.dict(os.environ, {"RSHUB_RELEASE_KEY_DIR": self.keys})
        p.start()
        self.addCleanup(p.stop)

    def test_keygen_sign_verify_roundtrip(self):
        _, out = run("keygen")
        pub = out.strip().splitlines()[-1]
        self.assertEqual(len(pub), 64)
        self.assertEqual(run("pubkey")[1].strip(), pub)
        f = os.path.join(self.tmp, "RsHub-Setup-2.1.0.exe")
        with open(f, "wb") as fh:
            fh.write(b"installer")
        run("sign", f, "--version", "2.1.0")
        self.assertEqual(os.path.getsize(f + ".sig"), 64)
        self.assertEqual(run("verify", f, f + ".sig", "--version", "2.1.0", "--pubkey", pub)[0], 0)
        self.assertEqual(run("verify", f, f + ".sig", "--version", "2.1.1", "--pubkey", pub)[0], 1)
        with open(f, "ab") as fh:
            fh.write(b"!")
        self.assertEqual(run("verify", f, f + ".sig", "--version", "2.1.0", "--pubkey", pub)[0], 1)
        # and what the tool signs is what the app accepts
        with open(f, "wb") as fh:
            fh.write(b"installer")
        with open(f + ".sig", "rb") as fh:
            sig = fh.read()
        import hashlib
        self.assertTrue(su.signature_ok(os.path.basename(f), "2.1.0", hashlib.sha256(b"installer").hexdigest(), sig, [pub]))

    def test_the_key_is_protected_not_plain(self):
        run("keygen")
        from rshub import ed25519
        seed = rs.load_seed()
        with open(os.path.join(self.keys, "release.key"), "rb") as fh:
            self.assertNotIn(seed, fh.read())
        self.assertEqual(len(ed25519.public_key(seed)), 32)

    def test_keygen_never_overwrites_and_no_backup_without_export(self):
        run("keygen")
        self.assertEqual(os.listdir(self.keys), ["release.key"])
        with self.assertRaises(SystemExit):
            run("keygen")

    def test_export_writes_a_backup_that_restores(self):
        backup = os.path.join(self.tmp, "backup.txt")
        _, out = run("keygen", "--export", backup)
        pub = out.strip().splitlines()[-1]
        self.assertIn("OFFLINE", out)
        os.remove(os.path.join(self.keys, "release.key"))
        run("restore", backup)
        self.assertEqual(run("pubkey")[1].strip(), pub)

    def test_the_key_and_backup_cannot_go_inside_the_repo(self):
        with mock.patch.dict(os.environ, {"RSHUB_RELEASE_KEY_DIR": os.path.join(rs.ROOT, "oops")}):
            with self.assertRaises(SystemExit):
                run("keygen")
        with self.assertRaises(SystemExit):
            run("keygen", "--export", os.path.join(rs.ROOT, "backup.txt"))
        self.assertFalse(os.path.exists(os.path.join(rs.ROOT, "oops")))
        self.assertFalse(os.path.exists(self.keys))


if __name__ == "__main__":
    unittest.main()

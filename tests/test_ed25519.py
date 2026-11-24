"""rshub/ed25519.py against every Ed25519 test vector of RFC 8032 section 7.1, plus rejection cases."""
import json
import os
import unittest

from rshub import ed25519

with open(os.path.join(os.path.dirname(__file__), "rfc8032_ed25519_vectors.json"), encoding="utf-8") as _fh:
    VECTORS = json.load(_fh)


class Rfc8032(unittest.TestCase):
    def test_all_five_vectors_are_there(self):
        self.assertEqual([v["name"] for v in VECTORS], ["1", "2", "3", "1024", "SHA(abc)"])

    def test_vectors(self):
        for v in VECTORS:
            with self.subTest(v["name"]):
                seed, pub = bytes.fromhex(v["secret"]), bytes.fromhex(v["public"])
                msg, sig = bytes.fromhex(v["message"]), bytes.fromhex(v["signature"])
                self.assertEqual(ed25519.public_key(seed), pub)
                self.assertEqual(ed25519.sign(seed, msg), sig)
                self.assertTrue(ed25519.verify(pub, msg, sig))

    def test_rejects_changes(self):
        v = VECTORS[1]
        pub, msg, sig = bytes.fromhex(v["public"]), bytes.fromhex(v["message"]), bytes.fromhex(v["signature"])
        self.assertFalse(ed25519.verify(pub, msg + b"x", sig))
        self.assertFalse(ed25519.verify(pub, b"", sig))
        for i in (0, 31, 32, 63):
            bad = bytearray(sig)
            bad[i] ^= 1
            self.assertFalse(ed25519.verify(pub, msg, bytes(bad)), i)
        other = bytes.fromhex(VECTORS[0]["public"])
        self.assertFalse(ed25519.verify(other, msg, sig))

    def test_rejects_malformed_input(self):
        v = VECTORS[0]
        pub, sig = bytes.fromhex(v["public"]), bytes.fromhex(v["signature"])
        self.assertFalse(ed25519.verify(pub, b"", sig[:63]))
        self.assertFalse(ed25519.verify(pub[:31], b"", sig))
        self.assertFalse(ed25519.verify(b"\xff" * 32, b"", sig))
        # s >= group order is not a valid (malleable) signature
        s = int.from_bytes(sig[32:], "little") + ed25519.Q
        self.assertFalse(ed25519.verify(pub, b"", sig[:32] + (s % 2 ** 256).to_bytes(32, "little")))
        self.assertFalse(ed25519.verify(pub, b"", None))

    def test_matches_the_cryptography_library_when_present(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("cryptography not installed")
        seed = os.urandom(32)
        k = Ed25519PrivateKey.from_private_bytes(seed)
        self.assertEqual(ed25519.sign(seed, b"hello"), k.sign(b"hello"))


if __name__ == "__main__":
    unittest.main()

"""settings.json under stress: a file that is valid JSON but not an object, reset racing another write, writers that
must never share a temp file. Nothing here touches the user's settings (a temp file stands in for them)."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from rshub import config, settings


class SettingsFile(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.path = os.path.join(d, "settings.json")
        p = mock.patch.object(config, "SETTINGS", self.path)
        p.start()
        self.addCleanup(p.stop)

    def put(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_a_file_that_is_not_an_object_reads_as_empty(self):
        """read() is what the start calls first (language, port, administrator mode): `[]` or `null` made it raise
        AttributeError on .get() and RsHub did not open at all."""
        for text in ("[]", "null", '"text"', "3"):
            self.put(text)
            self.assertEqual(settings.read(), {}, text)

    def test_the_installer_setup_survives_it_too(self):
        self.put("[1, 2]")
        self.assertTrue(settings.apply_setup({"language": "es"}))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"language": "es"})

    def test_reset_reads_under_the_lock(self):
        """reset() read the file before taking the lock: a change another thread saved between that read and its
        write was lost. Now the read and the write are one step."""
        self.put(json.dumps({"theme": "light"}))
        seen = []
        real = settings.load_strict

        def spy():
            seen.append(settings._lock.locked())
            return real()
        with mock.patch.object(settings, "load_strict", side_effect=spy):
            old, ok = settings.reset()
        self.assertTrue(ok)
        self.assertEqual(old, {"theme": "light"})
        self.assertTrue(seen and all(seen))

    def test_writers_never_share_a_temp_file_and_leave_none_behind(self):
        """All writers used settings.json.tmp: two at once (the installer's --setup and the app, two threads) replaced
        or removed each other's half-written file."""
        errors = []

        def w(i):
            try:
                for k in range(30):
                    settings.write_json(self.path, {"i": i, "k": k})
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn("i", settings.read())
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.path)) if f.endswith(".tmp")], [])

    def test_a_failed_write_removes_its_temp_file(self):
        with mock.patch("os.replace", side_effect=PermissionError(13, "in use")), mock.patch("time.sleep"):
            with self.assertRaises(PermissionError):
                settings.write_json(self.path, {"a": 1})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [])


if __name__ == "__main__":
    unittest.main()

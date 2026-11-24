import os
import unittest
from unittest import mock

from rshub.engine import fakepc, system


class FakePcGate(unittest.TestCase):
    def test_ignored_without_isolated(self):
        for k in fakepc.KINDS:
            for env in ({"RSHUB_FAKE_PC": k}, {"RSHUB_FAKE_PC": k, "RSHUB_ISOLATED": "0"}):
                with self.subTest(k=k, env=env), mock.patch.dict(os.environ, env, clear=False):
                    if "RSHUB_ISOLATED" not in env:
                        os.environ.pop("RSHUB_ISOLATED", None)
                    self.assertFalse(fakepc.active())
                    self.assertIsNone(fakepc.kind())

    def test_ignored_without_fake_value(self):
        for v in ("", "laptop", "phone", "handheld; rm"):
            with self.subTest(v), mock.patch.dict(os.environ, {"RSHUB_ISOLATED": "1", "RSHUB_FAKE_PC": v}):
                self.assertFalse(fakepc.active())

    def test_active_only_together(self):
        with mock.patch.dict(os.environ, {"RSHUB_ISOLATED": "1", "RSHUB_FAKE_PC": "desktop"}):
            self.assertTrue(fakepc.active())
            info = system.pc_info()
            self.assertEqual(info["type"], "desktop")
            self.assertFalse(any(c["id"] == "battery" for c in info["components"]))
            self.assertEqual(system.counts()["programs"], 74)
            self.assertIsNone(fakepc.telemetry({"battery": 80})["battery"])

    def test_handheld(self):
        with mock.patch.dict(os.environ, {"RSHUB_ISOLATED": "1", "RSHUB_FAKE_PC": " Handheld "}):
            self.assertEqual(fakepc.kind(), "handheld")
            info = system.pc_info()
            self.assertEqual(info["type"], "handheld")
            ids = [c["id"] for c in info["components"]]
            for need in ("cpu", "gpu", "ram", "ssd", "display", "battery", "wifi", "audio", "sys"):
                self.assertIn(need, ids)
            self.assertIn("Z1 Extreme", next(c for c in info["components"] if c["id"] == "cpu")["title"])
            disp = next(c for c in info["components"] if c["id"] == "display")
            self.assertTrue(disp["monitors"][0]["internal"])
            t = fakepc.telemetry({"battery": None, "ramTotal": 64, "vramTotal": 8})
            self.assertEqual((t["battery"], t["ramTotal"], t["vramTotal"]), (76, 15.3, 4))
            self.assertEqual(system.counts()["programs"], 41)

    def test_every_kind_has_the_same_shape(self):
        for k in fakepc.KINDS:
            with self.subTest(k), mock.patch.dict(os.environ, {"RSHUB_ISOLATED": "1", "RSHUB_FAKE_PC": k}):
                info = system.pc_info()
                self.assertEqual(info["type"], k)
                self.assertTrue(info["model"] and info["typeName"] and info["components"])
                has_bat = any(c["id"] == "battery" for c in info["components"])
                self.assertEqual(has_bat, fakepc.BATTERY.get(k, False))
                self.assertEqual(fakepc.telemetry({})["battery"] is not None, has_bat)

    def test_pc_info_not_faked_when_ignored(self):
        for k in fakepc.KINDS:
            with mock.patch.dict(os.environ, {"RSHUB_FAKE_PC": k}):
                os.environ.pop("RSHUB_ISOLATED", None)
                with mock.patch.object(system, "_mem", {"model": "Real", "typeName": "Laptop", "components": [], "ts": 9e12}):
                    self.assertEqual(system.pc_info()["model"], "Real")


if __name__ == "__main__":
    unittest.main()

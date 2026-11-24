"""Engine robustness found in round 8: odd answers from drivers, Windows on another drive letter, a damaged cache.
Nothing here touches the system: Windows' answers are mocked."""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from rshub.engine import system, telemetry


def _sampler():
    s = telemetry.Sampler.__new__(telemetry.Sampler)
    s._adapters, s._adapters_at = [("k1", "NVIDIA GeForce RTX 4060 Laptop GPU")], time.monotonic()
    s._pdh = mock.Mock(array=mock.Mock(return_value=[]))
    s._pstate, s._pstate_at = {}, time.monotonic()
    s._nv_last, s._dgpu_quiet_since, s.gpu_wanted = {}, None, True
    return s


class NvmlThatFails(unittest.TestCase):
    def test_a_failing_driver_call_does_not_end_the_whole_sample(self):
        s = _sampler()
        s._nv = mock.Mock(read=mock.Mock(side_effect=OSError("exception: access violation reading 0x0")))
        g = s._gpu(on_battery=False)
        self.assertEqual(g["state"], "active")
        self.assertIsNone(g.get("temp"))

    def test_a_driver_without_nvmlinit_v2_counts_as_missing(self):
        nv = telemetry._Nvml()
        nv.lib = mock.Mock(spec=[])            # no nvmlInit_v2 at all
        self.assertFalse(nv.init())
        self.assertTrue(nv.missing)
        self.assertIsNone(nv.read())


class WindowsOnAnotherDrive(unittest.TestCase):
    def test_no_c_drive_falls_back_to_the_system_drive(self):
        seen = []

        def usage(root):
            seen.append(root)
            if root.upper().startswith("C:"):
                raise FileNotFoundError(root)
            return mock.Mock(total=100, used=40, free=60)
        with mock.patch.dict(os.environ, {"SystemDrive": "D:"}), mock.patch.object(system.psutil, "disk_usage", side_effect=usage):
            self.assertEqual(system._sys_drive_usage().free, 60)
        self.assertEqual(seen[0], "D:\\")

    def test_no_drive_at_all_is_zero_not_an_error(self):
        with mock.patch.object(system.psutil, "disk_usage", side_effect=OSError("gone")):
            du = system._sys_drive_usage()
        self.assertEqual((du.total, du.used, du.free), (0, 0, 0))


class DamagedPcInfoCache(unittest.TestCase):
    def test_a_cache_that_is_not_an_object_is_measured_again(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        f = os.path.join(tmp, "pcinfo.json")
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(["not", "an", "object"], fh)
        with mock.patch.object(system, "_CACHE", f), mock.patch.object(system, "_mem", None), \
                mock.patch.object(system.fakepc, "active", return_value=False), \
                mock.patch.object(system, "pc_info_fresh", return_value={"model": "fresh"}) as fresh:
            self.assertEqual(system.pc_info(), {"model": "fresh"})
        fresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()

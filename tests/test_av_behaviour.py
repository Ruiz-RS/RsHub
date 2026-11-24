"""What antivirus behaviour engines (Kaspersky's System Watcher, issue #7; Defender; Bitdefender) watch, kept out of
RsHub's ordinary running: no "-ExecutionPolicy Bypass" or "-WindowStyle Hidden" PowerShell, no netsh.exe or
nvidia-smi.exe every few seconds (the Wi-Fi and the NVIDIA readings are made inside the process), no reading the memory
or duplicating the handles of Windows' core processes and security software, and the elevated start's scheduled-task
checks only when they can find something. Nothing here runs PowerShell, schtasks or a real scan."""
import ctypes
import os
import re
import tempfile
import unittest
from unittest import mock

from rshub import elevation
from rshub.engine import autopilot, monitor, system, telemetry, wlan
from rshub.engine.removal import cleaner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "rshub")
_REAL_NVIDIA_ASLEEP = telemetry.nvidia_asleep


def _sources():
    for dirpath, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "ui")]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(dirpath, f)
                with open(p, encoding="utf-8") as fh:
                    yield os.path.relpath(p, ROOT), fh.read()


class CommandLines(unittest.TestCase):
    def test_no_powershell_switches_that_droppers_use(self):
        """-ExecutionPolicy never applies to -Command (and Windows' own modules load under any policy); a process
        started with CREATE_NO_WINDOW has no window to hide. Both are what behaviour rules match on."""
        bad = re.compile(r'"-(ExecutionPolicy|ep|WindowStyle|w|EncodedCommand|enc|e)"', re.I)
        for path, text in _sources():
            for m in bad.finditer(text):
                self.fail(f"{path}: {m.group(0)}")

    def test_no_looping_console_tools_for_readings(self):
        for path, text in _sources():
            self.assertNotRegex(text, r'\["netsh", "wlan"', path)
            self.assertNotRegex(text, r'\["nvidia-smi"', path)


class Wifi(unittest.TestCase):
    def test_band(self):
        self.assertEqual(wlan.band(2437), "2.4 GHz")
        self.assertEqual(wlan.band(5220), "5 GHz")
        self.assertEqual(wlan.band(5955), "6 GHz")
        self.assertEqual(wlan.band(None, 6), "2.4 GHz")
        self.assertEqual(wlan.band(None, 44), "5 GHz")
        self.assertIsNone(wlan.band())

    def test_quality_from_rssi(self):
        self.assertEqual(wlan.quality_from_rssi(-57), 86)
        self.assertEqual(wlan.quality_from_rssi(-30), 100)
        self.assertEqual(wlan.quality_from_rssi(-110), 0)

    def test_structures_have_windows_sizes(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("64-bit layout")
        self.assertEqual(ctypes.sizeof(wlan._IFACE), 532)          # WLAN_INTERFACE_INFO
        self.assertEqual(ctypes.sizeof(wlan._CONNECTION), 604)     # WLAN_CONNECTION_ATTRIBUTES
        self.assertEqual(ctypes.sizeof(wlan._BSS), 360)            # WLAN_BSS_ENTRY

    def test_without_the_api_nothing_is_made_up(self):
        with mock.patch.object(wlan, "_lib", return_value=None):
            self.assertIsNone(wlan.current())
            self.assertIsNone(wlan.signal())
            self.assertIsNone(wlan.ssid())

    def test_a_real_answer_has_the_netsh_shape(self):
        """Whatever this PC has (no Wi-Fi, not connected, connected): the keys the Network page reads."""
        w = wlan.current()
        if w is None:
            self.skipTest("no Wi-Fi API here")
        self.assertIn("connected", w)
        if w["connected"]:
            for k in ("ssid", "signal", "band", "channel", "radio"):
                self.assertIn(k, w)
            self.assertTrue(0 <= w["signal"] <= 100)

    def test_callers_use_the_api_and_start_no_process(self):
        conn = {"connected": True, "ssid": "Home", "signal": 71, "band": "5 GHz", "channel": "44", "radio": "802.11ax"}
        with mock.patch.object(wlan, "current", return_value=conn), \
                mock.patch("subprocess.run", side_effect=AssertionError("no process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no process")):
            self.assertEqual(monitor._wifi(), conn)
            self.assertEqual(autopilot.wifi_ssid(), "Home")
            s = telemetry.Sampler.__new__(telemetry.Sampler)
            s._slow, s._wifi_probe = {}, (float("inf"), True)
            s._wifi()
            self.assertEqual(s._slow["wifiSignal"], 71)


class Nvidia(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(telemetry, "nvidia_asleep", return_value=False)   # this PC's own card may be asleep now
        p.start()
        self.addCleanup(p.stop)

    def test_a_card_that_is_asleep_is_not_woken_for_the_temperature(self):
        # the temperature trigger asks every 10 s: opening NVML each time kept an Optimus laptop's card awake on battery
        with mock.patch.object(telemetry, "nvidia_asleep", return_value=True), \
                mock.patch.object(telemetry, "nvml_once", side_effect=AssertionError("NVML wakes the card")):
            self.assertIsNone(autopilot.gpu_temp())
        with mock.patch.object(telemetry, "nvidia_asleep", return_value=True), \
                mock.patch.object(telemetry, "nvml_once", return_value={"temp": 41}):
            self.assertEqual(autopilot.gpu_temp(wake=True), 41.0)   # the one-off "is there a card" check

    def test_asleep_only_when_every_nvidia_adapter_is_powered_down(self):
        cases = [({"Intel(R) UHD Graphics": 1, "NVIDIA GeForce RTX 4060 Laptop GPU": 4}, True),
                 ({"Intel(R) UHD Graphics": 4, "NVIDIA GeForce RTX 4060 Laptop GPU": 1}, False),
                 ({"Intel(R) UHD Graphics": 1}, False), ({}, False)]
        for states, want in cases:
            with mock.patch.object(telemetry, "_display_power_states", return_value=states):
                self.assertIs(_REAL_NVIDIA_ASLEEP(), want, states)

    def test_temperature_and_memory_through_nvml(self):
        with mock.patch.object(telemetry, "nvml_once", return_value={"temp": 54, "vramTotal": 8}), \
                mock.patch("subprocess.run", side_effect=AssertionError("no nvidia-smi")):
            self.assertEqual(autopilot.gpu_temp(), 54.0)
            self.assertEqual(system._gpu_vram("NVIDIA GeForce RTX 4060"), 8)
            self.assertIsNone(system._gpu_vram("Intel UHD Graphics"))
        with mock.patch.object(telemetry, "nvml_once", return_value=None):
            self.assertIsNone(autopilot.gpu_temp())
            self.assertIsNone(system._gpu_vram("NVIDIA GeForce RTX 4060"))


class CleanerInUse(unittest.TestCase):
    def proc(self, pid, name, cwd):
        p = mock.Mock(pid=pid, info={"name": name, "exe": None, "cmdline": []})
        p.cwd.return_value = cwd
        p.open_files.return_value = [mock.Mock(path=os.path.join(cwd, "open.bin"))]
        return p

    def test_core_and_security_processes_are_never_opened(self):
        tool = self.proc(-2, "tool.exe", r"C:\Work\tool")
        guarded = [self.proc(-3, "lsass.exe", r"C:\Windows\System32"), self.proc(-4, "avp.exe", r"C:\Kaspersky"),
                   self.proc(-5, "MsMpEng.exe", r"C:\ProgramData\Defender")]
        with mock.patch("psutil.process_iter", return_value=[tool] + guarded):
            got = cleaner.in_use_paths(budget=60)
        self.assertIn(cleaner._norm(r"C:\Work\tool"), got)
        self.assertIn(cleaner._norm(r"C:\Work\tool\open.bin"), got)
        for p in guarded:
            p.cwd.assert_not_called()
            p.open_files.assert_not_called()

    def test_the_list_is_lower_case(self):
        self.assertTrue(all(n == n.lower() for n in cleaner.NOT_INSPECTED))
        self.assertFalse(cleaner._inspectable("LSASS.EXE"))
        self.assertTrue(cleaner._inspectable("python.exe"))


class ElevatedStart(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="sh-admin-checks-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.stamp = os.path.join(d, "admin-checks")
        p = mock.patch.object(elevation, "CHECKS_STAMP", self.stamp)
        p.start()
        self.addCleanup(p.stop)

    def run_start(self, per_machine=False, argv=("RsHub.exe",), ok=True):
        with mock.patch.object(elevation, "uses_admin_task", return_value=per_machine), \
                mock.patch.object(elevation, "sync_admin_task", return_value=ok) as sync, \
                mock.patch.object(elevation, "repair_update_task", return_value=True) as repair, \
                mock.patch.object(elevation.sys, "argv", list(argv)):
            elevation.admin_maintenance()
        return sync.call_count, repair.call_count

    def test_leftovers_are_looked_for_once_per_version(self):
        self.assertEqual(self.run_start(), (1, 1))
        self.assertEqual(self.run_start(), (0, 0))   # the next elevated starts start no schtasks.exe
        with open(self.stamp, "w", encoding="utf-8") as fh:
            fh.write("1.0.0|old")                      # another version (or another folder): once more
        self.assertEqual(self.run_start(), (1, 1))

    def test_a_failed_check_is_tried_again(self):
        self.assertEqual(self.run_start(ok=False), (1, 1))
        self.assertEqual(self.run_start(), (1, 1))
        self.assertEqual(self.run_start(), (0, 0))

    def test_an_all_users_install_checks_its_task_unless_the_task_opened_it(self):
        self.run_start(per_machine=True)
        self.assertEqual(self.run_start(per_machine=True), (1, 0))
        self.assertEqual(self.run_start(per_machine=True, argv=("RsHub.exe", "--from-task")), (0, 0))

    def test_a_task_that_is_right_is_not_made_again(self):
        with mock.patch.object(elevation, "uses_admin_task", return_value=True), \
                mock.patch.object(elevation.settings, "read", return_value={"alwaysAdmin": True}), \
                mock.patch.object(elevation, "admin_task_current", return_value=True), \
                mock.patch.object(elevation, "ensure_admin_task") as make:
            self.assertTrue(elevation.sync_admin_task())
        make.assert_not_called()

    def test_admin_task_current_reads_one_definition(self):
        from rshub import schtask
        exe, args = elevation.config.launch_cmd("--from-task")
        good = schtask.task_xml(exe, args)
        with mock.patch.object(schtask, "query_xml", return_value=good) as q:
            self.assertTrue(elevation.admin_task_current())
        q.assert_called_once()
        old = good.replace("<Priority>4</Priority>", "<Priority>7</Priority>")
        with mock.patch.object(schtask, "query_xml", return_value=old):
            self.assertFalse(elevation.admin_task_current())
        with mock.patch.object(schtask, "query_xml", return_value=schtask.task_xml(r"C:\Other\RsHub.exe")):
            self.assertFalse(elevation.admin_task_current())
        with mock.patch.object(schtask, "query_xml", return_value=""):
            self.assertFalse(elevation.admin_task_current())


if __name__ == "__main__":
    unittest.main()

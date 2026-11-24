"""CPU temperature from hardware monitors the user runs (engine/sensors.py), against fake shared memory blocks laid out
like HWiNFO's and Core Temp's. Nothing is installed or started."""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import unittest
from unittest import mock

from rshub.engine import sensors

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileMappingW.restype = wt.HANDLE
k32.CreateFileMappingW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]


class Shared:
    """A named shared memory block holding `data` while the test runs."""

    def __init__(self, data):
        self.name = f"Local\\RsHubTest_{os.getpid()}_{id(self)}"
        self.h = k32.CreateFileMappingW(wt.HANDLE(-1), None, 0x04, 0, len(data), self.name)  # PAGE_READWRITE
        p = k32.MapViewOfFile(self.h, 0x0002, 0, 0, len(data))  # FILE_MAP_WRITE
        ctypes.memmove(p, data, len(data))
        k32.UnmapViewOfFile(p)

    def close(self):
        k32.CloseHandle(self.h)


def hwinfo_block(readings):
    """HWiNFO SM2: header, then reading elements (type, sensor, id, label, user label, unit, value, min, max, avg)."""
    size_r, off_r = 316, 44
    head = b"SiWH" + struct.pack("<IIq", 2, 0, 0) + struct.pack("<IIIIII", 44, 0, 0, off_r, size_r, len(readings))
    body = b""
    for kind, label, value in readings:
        lab = label.encode()[:127].ljust(128, b"\0")
        body += struct.pack("<III", kind, 0, 0) + lab + lab + "°C".encode("latin-1").ljust(16, b"\0") + struct.pack("<dddd", value, 0, 0, 0)
    return head + body


def coretemp_block(temps, delta=False, tjmax=100, fahrenheit=False):
    d = bytearray(2686)
    struct.pack_into("<I", d, 1024, tjmax)
    struct.pack_into("<II", d, 1536, len(temps), 1)
    struct.pack_into(f"<{len(temps)}f", d, 1544, *temps)
    d[2684], d[2685] = int(fahrenheit), int(delta)
    return bytes(d)


class Sensors(unittest.TestCase):
    def test_hwinfo_prefers_the_cpu_package(self):
        blk = Shared(hwinfo_block([(1, "GPU Temperature", 51.0), (2, "CPU Package Power", 40.0),
                                   (1, "Core Max", 66.0), (1, "CPU Package", 63.5)]))
        self.addCleanup(blk.close)
        with mock.patch.object(sensors, "HWINFO_NAME", blk.name):
            self.assertEqual(sensors.hwinfo(), 64)

    def test_hwinfo_missing_or_closed(self):
        with mock.patch.object(sensors, "HWINFO_NAME", "Local\\RsHubNoSuchBlock"):
            self.assertIsNone(sensors.hwinfo())
        blk = Shared(b"DAED" + bytes(40))  # HWiNFO marks a closed block
        self.addCleanup(blk.close)
        with mock.patch.object(sensors, "HWINFO_NAME", blk.name):
            self.assertIsNone(sensors.hwinfo())

    def test_coretemp_hottest_core_and_distance_to_tjmax(self):
        a = Shared(coretemp_block([48.0, 55.0, 51.0, 50.0]))
        self.addCleanup(a.close)
        with mock.patch.object(sensors, "CORETEMP_NAMES", (a.name,)):
            self.assertEqual(sensors.coretemp(), 55)
        b = Shared(coretemp_block([40.0, 35.0], delta=True, tjmax=100))  # 60 °C and 65 °C
        self.addCleanup(b.close)
        with mock.patch.object(sensors, "CORETEMP_NAMES", (b.name,)):
            self.assertEqual(sensors.coretemp(), 65)
        c = Shared(coretemp_block([140.0], fahrenheit=True))  # 60 °C
        self.addCleanup(c.close)
        with mock.patch.object(sensors, "CORETEMP_NAMES", (c.name,)):
            self.assertEqual(sensors.coretemp(), 60)

    def test_cpu_temp_names_its_source(self):
        a = Shared(coretemp_block([57.0]))
        self.addCleanup(a.close)
        with mock.patch.object(sensors, "HWINFO_NAME", "Local\\RsHubNoSuchBlock"), \
                mock.patch.object(sensors, "CORETEMP_NAMES", (a.name,)):
            self.assertEqual(sensors.cpu_temp(max_age=0), (57, "Core Temp"))
            self.assertEqual(sensors.peek(), (57, "Core Temp"))


if __name__ == "__main__":
    unittest.main()

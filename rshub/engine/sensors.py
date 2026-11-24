"""The processor temperature from hardware monitors the user already runs.

Windows has no standard way to read the CPU's own temperature: RsHub reads the ACPI thermal zones (telemetry.py),
which many desktop boards don't provide. Reading the CPU's sensors directly needs a kernel driver, which RsHub
won't install (antivirus software flags them, and Microsoft blocks several as vulnerable). So when the thermal zones
give nothing, RsHub takes the reading from a monitor that is already running, if there is one:

- HWiNFO, with "Shared Memory Support" on (Global\\HWiNFO_SENS_SM2)
- Core Temp (its shared memory, CoreTempMappingObject)
- LibreHardwareMonitor or OpenHardwareMonitor (their WMI namespaces)

Everything here is read-only and quick; nothing is installed or started.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import time

FILE_MAP_READ = 0x0004
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenFileMappingW.restype = wt.HANDLE
_k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_k32.MapViewOfFile.restype = ctypes.c_void_p
_k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
_k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wt.HANDLE]

HWINFO_NAME = "Global\\HWiNFO_SENS_SM2"  # tests point these at their own fake blocks
CORETEMP_NAMES = ("CoreTempMappingObjectEx", "CoreTempMappingObject")
_ok = lambda c: c is not None and 15 < c < 125  # noqa: E731  (a plausible processor temperature)


def _read_shared(name, size):
    """The first `size` bytes of a named shared memory block, or None if nobody publishes it."""
    h = _k32.OpenFileMappingW(FILE_MAP_READ, False, name)
    if not h:
        return None
    try:
        p = _k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not p:
            return None
        try:
            return ctypes.string_at(p, size)
        finally:
            _k32.UnmapViewOfFile(p)
    finally:
        _k32.CloseHandle(h)


def hwinfo():
    """HWiNFO's shared memory: the CPU package (or Tctl/Tdie on AMD) temperature."""
    head = _read_shared(HWINFO_NAME, 44)
    if not head or head[:4] not in (b"SiWH", b"HWiS"):
        return None
    off_r, size_r, num_r = struct.unpack_from("<III", head, 32)
    if not (0 < num_r < 10000 and 300 <= size_r < 4096):
        return None
    data = _read_shared(HWINFO_NAME, off_r + size_r * num_r)
    if not data:
        return None
    best = None
    for i in range(num_r):
        o = off_r + i * size_r
        kind = struct.unpack_from("<I", data, o)[0]
        if kind != 1:  # 1 = temperature
            continue
        label = data[o + 12:o + 140].split(b"\0", 1)[0].decode("latin-1", "replace").lower()
        value = struct.unpack_from("<d", data, o + 284)[0]
        rank = 0 if "cpu package" in label or "tctl/tdie" in label else 1 if label.startswith("cpu (") or label == "cpu" else \
            2 if "core max" in label else None
        if rank is not None and _ok(value) and (best is None or rank < best[0]):
            best = (rank, value)
    return round(best[1]) if best else None


def coretemp():
    """Core Temp's shared memory: the hottest core."""
    for name in CORETEMP_NAMES:
        d = _read_shared(name, 2686)
        if not d:
            continue
        cores, cpus = struct.unpack_from("<II", d, 1536)
        n = cores * max(1, cpus)
        if not 0 < n <= 256:
            continue
        temps = struct.unpack_from(f"<{n}f", d, 1544)
        fahrenheit, delta = d[2684], d[2685]
        if delta:  # values are the distance to TjMax
            tj = struct.unpack_from("<128I", d, 1024)  # one TjMax per processor package
            temps = [tj[min(i // max(1, cores), 127)] - t for i, t in enumerate(temps)]
        if fahrenheit:
            temps = [(t - 32) * 5 / 9 for t in temps]
        temps = [t for t in temps if _ok(t)]
        if temps:
            return round(max(temps))
    return None


_wmi_off = {}  # namespace -> when it was found missing (WMI queries for a missing namespace are slow)


def wmi_monitor():
    """LibreHardwareMonitor / OpenHardwareMonitor publish their sensors in WMI while they run."""
    for ns, name in (("root\\LibreHardwareMonitor", "LibreHardwareMonitor"), ("root\\OpenHardwareMonitor", "OpenHardwareMonitor")):
        if time.monotonic() - _wmi_off.get(ns, -1e9) < 60:
            continue
        try:
            import clr  # noqa: F401
            clr.AddReference("System.Management")
            from System.Management import ManagementObjectSearcher
            en = ManagementObjectSearcher(ns, "SELECT Name, Value, Parent FROM Sensor WHERE SensorType='Temperature'").Get().GetEnumerator()
            best = None
            while en.MoveNext():  # never list()/len() on a WMI collection through pythonnet
                o = en.Current
                parent, label = str(o["Parent"] or "").lower(), str(o["Name"] or "").lower()
                if "cpu" not in parent:
                    continue
                v = float(o["Value"])
                rank = 0 if "package" in label or "tctl" in label or "tdie" in label else 1 if "max" in label else 2
                if _ok(v) and (best is None or rank < best[0]):
                    best = (rank, v)
            if best:
                return round(best[1]), name
            _wmi_off[ns] = time.monotonic()
        except Exception:
            _wmi_off[ns] = time.monotonic()
    return None


_last = {"at": -1e9, "val": (None, None)}


def peek(max_age=8.0):
    """The last reading, if recent (the slow telemetry loop refreshes it)."""
    return _last["val"] if time.monotonic() - _last["at"] < max_age else (None, None)


def cpu_temp(max_age=2.0):
    """(°C, source name) from a running hardware monitor, or (None, None). Cached for a couple of seconds."""
    if time.monotonic() - _last["at"] < max_age:
        return _last["val"]
    val = (None, None)
    for fn, name in ((hwinfo, "HWiNFO"), (coretemp, "Core Temp")):
        try:
            t = fn()
        except Exception:
            t = None
        if t is not None:
            val = (t, name)
            break
    else:
        try:
            r = wmi_monitor()
        except Exception:
            r = None
        if r:
            val = r
    _last.update(at=time.monotonic(), val=val)
    return val

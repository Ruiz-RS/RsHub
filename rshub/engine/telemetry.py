"""Real live telemetry for the PC. A thread measures every `rate` seconds and hands a dict to `push`.

Sources (all read-only, no extra processes in the hot path):
- psutil: CPU per thread, RAM, disk and network throughput.
- Windows performance counters (PDH): real CPU frequency (% Processor Performance, because psutil.cpu_freq()
  only returns the nominal clock on Windows), GPU usage per adapter and dedicated VRAM (the same numbers Task
  Manager shows: busiest engine per adapter), and ACPI thermal zones.
- NVML (nvml.dll through ctypes): NVIDIA temperature, power, clock and fan. It replaces a looping nvidia-smi
  process (~116 ms and a whole process per query). NVML is only queried while the dedicated GPU is awake (D0,
  read through SetupAPI, which does not wake it) and, on battery, only while it is actually busy: polling an
  idle dGPU keeps it out of D3cold on Optimus laptops and drains the battery (the same problem KDE and other
  monitors have with nvidia-smi).
- Battery driver IOCTLs: charge/discharge rate, design and full-charge capacity, cycle count (0.2 ms, against
  ~190 ms of CPU for the equivalent WMI query).
- the Wi-Fi API (wlanapi.dll, engine/wlan.py) for the Wi-Fi signal and a short TCP connection for latency.

CPU package temperature: Windows only exposes ACPI thermal zones. Many laptops report a fixed placeholder there
(27.8 °C), so implausible values are discarded. Real CPU sensors need a kernel driver such as WinRing0, which
Microsoft Defender now flags as a vulnerable driver (CVE-2020-14979); RsHub deliberately does not ship one.
Anything Windows does not provide is sent as None: the UI shows "—" instead of making it up.
"""
import ctypes
import re
import socket
import threading
import time
from ctypes import wintypes

import psutil

from . import fakepc

NO_WINDOW = 0x08000000
GB = 1024 ** 3
_PDH_FMT_DOUBLE = 0x00000200


# ---------- performance counters ----------
class _PdhVal(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("dbl", ctypes.c_double)]


class _PdhItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _PdhVal)]


class _Pdh:
    """One PDH query with several counters, single or multi-instance (\\GPU Engine(*)\\...)."""

    def __init__(self, paths):
        self.ok = False
        self.c = {}
        try:
            self.pdh = ctypes.windll.pdh
            self.q = wintypes.HANDLE()
            if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) != 0:
                return
            for key, path in paths.items():
                h = wintypes.HANDLE()
                if self.pdh.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h)) == 0:
                    self.c[key] = h
            self.pdh.PdhCollectQueryData(self.q)
            self.ok = bool(self.c)
        except Exception:
            pass

    def collect(self):
        return self.ok and self.pdh.PdhCollectQueryData(self.q) == 0

    def value(self, key):
        h = self.c.get(key)
        if h is None:
            return None
        v = _PdhVal()
        if self.pdh.PdhGetFormattedCounterValue(h, _PDH_FMT_DOUBLE, None, ctypes.byref(v)) != 0:
            return None
        return v.dbl

    def array(self, key, nonzero=False):
        """[(instance, value)]. nonzero=True skips zeros before decoding their names (GPU Engine has 1000+ instances)."""
        h = self.c.get(key)
        if h is None:
            return []
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        self.pdh.PdhGetFormattedCounterArrayW(h, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(h, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
        out = []
        for i in range(count.value):
            fv = items[i].FmtValue
            if fv.CStatus in (0, 1) and (fv.dbl > 0 or not nonzero):
                out.append((items[i].szName, fv.dbl))
        return out


class _PerfInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t), ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t), ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t), ("HandleCount", wintypes.DWORD),
                ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]


def _perf_info():
    pi = _PerfInfo()
    pi.cb = ctypes.sizeof(pi)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return None
    return pi


# ---------- graphics adapters: LUID -> name (D3DKMT, does not wake the GPU) ----------
class _LUID(ctypes.Structure):
    _fields_ = [("Low", wintypes.DWORD), ("High", wintypes.LONG)]


class _ADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("AdapterLuid", _LUID), ("NumOfSources", wintypes.ULONG), ("bPrecise", wintypes.BOOL)]


class _ENUM2(ctypes.Structure):
    _fields_ = [("NumAdapters", wintypes.ULONG), ("pAdapters", ctypes.POINTER(_ADAPTERINFO))]


class _QUERY(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", ctypes.c_int), ("pData", ctypes.c_void_p), ("Size", wintypes.UINT)]


class _REGINFO(ctypes.Structure):
    _fields_ = [("AdapterString", wintypes.WCHAR * 260), ("BiosString", wintypes.WCHAR * 260),
                ("DacType", wintypes.WCHAR * 260), ("ChipType", wintypes.WCHAR * 260)]


_RE_LUID = re.compile(r"luid_0x([0-9a-f]+)_0x([0-9a-f]+)(?:_phys_\d+_eng_(\d+))?", re.I)


def _luid_key(s):
    m = _RE_LUID.search(s)
    return f"{int(m.group(1), 16):08x}{int(m.group(2), 16):08x}" if m else None


def _adapters():
    """[(luid_key, name)] of the graphics adapters, without opening a D3D device."""
    try:
        gdi = ctypes.WinDLL("gdi32")
        e = _ENUM2()
        if gdi.D3DKMTEnumAdapters2(ctypes.byref(e)) != 0 or not e.NumAdapters:
            return []
        arr = (_ADAPTERINFO * e.NumAdapters)()
        e.pAdapters = arr
        if gdi.D3DKMTEnumAdapters2(ctypes.byref(e)) != 0:
            return []
        out = []
        for a in arr[:e.NumAdapters]:
            ri = _REGINFO()
            q = _QUERY(a.hAdapter, 8, ctypes.cast(ctypes.byref(ri), ctypes.c_void_p), ctypes.sizeof(ri))  # ADAPTERREGISTRYINFO
            name = ri.AdapterString if gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q)) == 0 else ""
            out.append((f"{a.AdapterLuid.High & 0xFFFFFFFF:08x}{a.AdapterLuid.Low:08x}", name))
            gdi.D3DKMTCloseAdapter(ctypes.byref(wintypes.UINT(a.hAdapter)))
        return out
    except Exception:
        return []


_VIRTUAL_NIC = re.compile(r"vethernet|hyper-v|tap-|tap |wintun|wireguard|tunnel|wan miniport|isatap|teredo|virtual|vpn|"
                          r"zerotier|tailscale|loopback|npcap|kernel debug|pseudo", re.I)
_VIRTUAL_GPU = ("remote display", "virtual", "parsec", "indirect", "basic display", "basic render", "citrix", "vmware",
                "virtualbox", "hyper-v", "spacedesk", "displaylink", "idd ")


def gpu_kind(name):
    """'discrete', 'integrated' or 'virtual' from the adapter's name. Intel "Arc Graphics" / "Arc 140V" (Core Ultra)
    and Radeon 780M / Vega 8 (Ryzen) are integrated even though the names look like graphics cards."""
    n = (name or "").lower()
    if not n or any(k in n for k in _VIRTUAL_GPU):
        return "virtual"
    if "nvidia" in n:
        return "discrete"
    if "intel" in n:
        return "discrete" if re.search(r"arc(\(tm\))?\s+(pro\s+)?[ab]\d{3}", n) else "integrated"
    if "radeon" in n or "amd" in n:
        if re.search(r"radeon(\(tm\))?\s*(rx|pro|vii|r[579]\b|hd\s*\d{4})|firepro|instinct", n):
            return "discrete"
        return "integrated"
    return "discrete"


def gpu_rank(name):
    """Which discrete card is the main one: NVIDIA, then AMD, then Intel Arc, then anything else."""
    n = (name or "").lower()
    return 0 if "nvidia" in n else 1 if ("radeon" in n or "amd" in n) else 2 if "intel" in n else 3


def _is_integrated(name):
    return gpu_kind(name) == "integrated"


# ---------- PnP power state of the display adapters (SetupAPI: read from the PnP manager, no wake-up) ----------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _DEVINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID), ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


_GUID_DISPLAY = _GUID(0x4d36e968, 0xe325, 0x11ce, (ctypes.c_ubyte * 8)(0xbf, 0xc1, 0x08, 0x00, 0x2b, 0xe1, 0x03, 0x18))


def _display_power_states():
    """{device name: D-state} (1 = D0 working ... 4 = D3 off). Empty dict if it cannot be read."""
    out = {}
    try:
        sa = ctypes.WinDLL("setupapi")
        sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
        sa.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DEVINFO)]
        sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DEVINFO), wintypes.DWORD,
                                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        sa.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        h = sa.SetupDiGetClassDevsW(ctypes.byref(_GUID_DISPLAY), None, None, 0x2)  # DIGCF_PRESENT
        if not h or h == ctypes.c_void_p(-1).value:
            return out
        i = 0
        while True:
            d = _DEVINFO()
            d.cbSize = ctypes.sizeof(d)
            if not sa.SetupDiEnumDeviceInfo(h, i, ctypes.byref(d)):
                break
            name = ctypes.create_unicode_buffer(256)
            sa.SetupDiGetDeviceRegistryPropertyW(h, ctypes.byref(d), 0x0, None, name, 512, None)  # SPDRP_DEVICEDESC
            pdata = (ctypes.c_ubyte * 64)()
            if sa.SetupDiGetDeviceRegistryPropertyW(h, ctypes.byref(d), 0x1E, None, pdata, 64, None):  # SPDRP_DEVICE_POWER_DATA
                out[name.value] = int.from_bytes(bytes(pdata[4:8]), "little")  # CM_POWER_DATA.PD_MostRecentPowerState
            i += 1
        sa.SetupDiDestroyDeviceInfoList(h)
    except Exception:
        pass
    return out


# ---------- NVML (NVIDIA Management Library) through ctypes ----------
class _NvUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _Nvml:
    """Lazy NVML session. init() costs ~40 ms once; each read ~0.05 ms (vs ~116 ms per nvidia-smi run)."""

    def __init__(self):
        self.lib = None
        self.dev = None
        self.missing = False
        self._lock = threading.RLock()  # shutdown (from whoever hides the window) must not cut a read short

    def init(self):
        if self.dev is not None:
            return True
        if self.missing:
            return False
        try:
            if self.lib is None:
                try:
                    self.lib = ctypes.WinDLL("nvml.dll")
                except OSError:
                    self.lib = ctypes.WinDLL(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll")
            if self.lib.nvmlInit_v2() != 0:
                return False
            dev = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) != 0:
                self.lib.nvmlShutdown()
                return False
            self.dev = dev
            return True
        except (OSError, AttributeError):
            self.missing = True  # no NVIDIA driver on this PC (or one too old to have nvmlInit_v2)
            return False

    def shutdown(self):
        with self._lock:
            if self.dev is not None and self.lib is not None:
                try:
                    self.lib.nvmlShutdown()
                except OSError:
                    pass
            self.dev = None

    def read(self):
        with self._lock:
            return self._read()

    def _read(self):
        if not self.init():
            return None
        L, d = self.lib, self.dev
        u, temp, power, clock, fan, mem = _NvUtil(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), _NvMem()
        ok = lambda rc: rc == 0
        return {
            "gpu": u.gpu if ok(L.nvmlDeviceGetUtilizationRates(d, ctypes.byref(u))) else None,
            "temp": temp.value if ok(L.nvmlDeviceGetTemperature(d, 0, ctypes.byref(temp))) else None,
            "power": round(power.value / 1000) if ok(L.nvmlDeviceGetPowerUsage(d, ctypes.byref(power))) else None,
            "clock": round(clock.value / 1000, 2) if ok(L.nvmlDeviceGetClockInfo(d, 0, ctypes.byref(clock))) else None,
            "fan": fan.value if ok(L.nvmlDeviceGetFanSpeed(d, ctypes.byref(fan))) else None,
            "vramTotal": round(mem.total / GB) if ok(L.nvmlDeviceGetMemoryInfo(d, ctypes.byref(mem))) else None,
            "vramUsed": round(mem.used / GB, 2) if mem.total else None,
        }


def nvidia_asleep():
    """True when every NVIDIA display adapter is powered down (D3), read through SetupAPI without waking it. Opening
    NVML then wakes the card (on an Optimus laptop it stays out of D3cold while it is asked every few seconds), and a
    sleeping card is cool anyway. False when there is none or the state can't be read."""
    nv = [s for n, s in _display_power_states().items() if "nvidia" in (n or "").lower()]
    return bool(nv) and all(s > 1 for s in nv)


def nvml_once():
    """One NVML reading (temperature, memory, ...) for callers outside the sampler, or None without an NVIDIA driver."""
    nv = _Nvml()
    try:
        return nv.read()
    finally:
        nv.shutdown()


# ---------- battery through its driver (IOCTL_BATTERY_*) ----------
class _BQI(ctypes.Structure):
    _fields_ = [("BatteryTag", wintypes.ULONG), ("InformationLevel", ctypes.c_int), ("AtRate", wintypes.LONG)]


class _BINFO(ctypes.Structure):
    _fields_ = [("Capabilities", wintypes.ULONG), ("Technology", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte * 3),
                ("Chemistry", ctypes.c_ubyte * 4), ("DesignedCapacity", wintypes.ULONG), ("FullChargedCapacity", wintypes.ULONG),
                ("DefaultAlert1", wintypes.ULONG), ("DefaultAlert2", wintypes.ULONG), ("CriticalBias", wintypes.ULONG),
                ("CycleCount", wintypes.ULONG)]


class _BWS(ctypes.Structure):
    _fields_ = [("BatteryTag", wintypes.ULONG), ("Timeout", wintypes.ULONG), ("PowerState", wintypes.ULONG),
                ("LowCapacity", wintypes.ULONG), ("HighCapacity", wintypes.ULONG)]


class _BSTATUS(ctypes.Structure):
    _fields_ = [("PowerState", wintypes.ULONG), ("Capacity", wintypes.ULONG), ("Voltage", wintypes.ULONG), ("Rate", wintypes.LONG)]


class _SPIFACE(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


_GUID_DEVICE_BATTERY = _GUID(0x72631e54, 0x78A4, 0x11d0, (ctypes.c_ubyte * 8)(0xbc, 0xf7, 0x00, 0xaa, 0x00, 0xb7, 0xb3, 0x2a))
_UNKNOWN_RATE = -0x80000000  # BATTERY_UNKNOWN_RATE


def _battery_paths():
    out = []
    try:
        sa = ctypes.WinDLL("setupapi")
        sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
        sa.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.POINTER(_SPIFACE)]
        sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SPIFACE), ctypes.c_void_p, wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        sa.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        h = sa.SetupDiGetClassDevsW(ctypes.byref(_GUID_DEVICE_BATTERY), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
        if not h or h == ctypes.c_void_p(-1).value:
            return out
        i = 0
        while True:
            d = _SPIFACE()
            d.cbSize = ctypes.sizeof(d)
            if not sa.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(_GUID_DEVICE_BATTERY), i, ctypes.byref(d)):
                break
            need = wintypes.DWORD()
            sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(d), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(max(need.value, 16))
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(d), buf, need, None, None):
                out.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            i += 1
        sa.SetupDiDestroyDeviceInfoList(h)
    except Exception:
        pass
    return out


def _battery_read(path):
    k32 = ctypes.WinDLL("kernel32")
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    hb = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x80, None)
    if not hb or hb == wintypes.HANDLE(-1).value:
        return None
    try:
        tag, wait, n = wintypes.ULONG(), wintypes.ULONG(0), wintypes.DWORD()
        if not k32.DeviceIoControl(hb, 0x294040, ctypes.byref(wait), 4, ctypes.byref(tag), 4, ctypes.byref(n), None) or not tag.value:
            return None
        q, bi = _BQI(tag.value, 0, 0), _BINFO()
        info = k32.DeviceIoControl(hb, 0x294044, ctypes.byref(q), ctypes.sizeof(q), ctypes.byref(bi), ctypes.sizeof(bi), ctypes.byref(n), None)
        ws, st = _BWS(tag.value, 0, 0, 0, 0), _BSTATUS()
        stat = k32.DeviceIoControl(hb, 0x29404C, ctypes.byref(ws), ctypes.sizeof(ws), ctypes.byref(st), ctypes.sizeof(st), ctypes.byref(n), None)
        return {"design": bi.DesignedCapacity if info else 0, "full": bi.FullChargedCapacity if info else 0,
                "cycles": bi.CycleCount if info else 0, "rate": st.Rate if stat and st.Rate != _UNKNOWN_RATE else None}
    finally:
        k32.CloseHandle(hb)


class Sampler:
    def __init__(self, push):
        self.push = push
        self.rate = 1.0
        self.active = True          # window visible or widget open
        self.gpu_wanted = True      # NVML only while someone is looking at GPU data
        self.lite = False           # on battery with the UI in its lighter mode: fewer slow extras (ping, Wi-Fi)
        self._stop = threading.Event()
        self._wake_slow = threading.Event()
        self._wake = threading.Event()   # set when the sampler becomes active again: no polling while nobody looks
        self._wifi_probe = (0.0, True)
        self._slow = {"wifiSignal": None, "ping": None}
        self._pdh = _Pdh({
            "perf": r"\Processor Information(_Total)\% Processor Performance",
            "eng": r"\GPU Engine(*)\Utilization Percentage",
            "ded": r"\GPU Adapter Memory(*)\Dedicated Usage",
            "tz": r"\Thermal Zone Information(*)\High Precision Temperature",
            "rx": r"\Network Interface(*)\Bytes Received/sec",
            "tx": r"\Network Interface(*)\Bytes Sent/sec",
        })
        self._base_mhz = self._base_clock()
        self._last_io = None
        self._adapters = []
        self._adapters_at = 0
        self._nv = _Nvml()
        self._nv_last = {}
        self._dgpu_quiet_since = None
        self._pstate = {}
        self._pstate_at = 0
        self._bat_path = None
        self._bat_paths_at = 0
        self._bat_static = {}

    # ---------- startup and control ----------
    def start(self):
        if getattr(self, "_started", False):
            return  # 'loaded' fires on every page reload: a single sampler
        self._started = True
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        threading.Thread(target=self._loop, daemon=True, name="tele").start()
        threading.Thread(target=self._slow_loop, daemon=True, name="tele-slow").start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._wake_slow.set()
        self._nv.shutdown()

    def set_rate(self, seconds):
        self.rate = max(0.25, min(5.0, float(seconds)))

    def set_active(self, active, gpu=True):
        self.active, self.gpu_wanted = bool(active), bool(gpu)
        if self.active:
            self._wake.set()
            self._wake_slow.set()
        if not (active and gpu):
            self._nv.shutdown()  # release the GPU so it can power down

    # ---------- main loop ----------
    def _loop(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            if self.active:
                try:
                    self.push(self.sample())
                except Exception as e:
                    print("telemetry:", e)
            if self.active:
                self._stop.wait(max(0.05, self.rate - (time.monotonic() - t0)))
            else:  # hidden: sleep until set_active() wakes it instead of waking up every 2 s to do nothing
                self._wake.wait(30.0)
                self._wake.clear()

    def sample(self):
        now = time.monotonic()
        per = psutil.cpu_percent(None, percpu=True)
        cpu = sum(per) / len(per) if per else psutil.cpu_percent(None)
        self._pdh.collect()
        perf = self._pdh.value("perf")
        freq = (self._base_mhz * perf / 100 / 1000) if (perf and self._base_mhz) else None
        vm = psutil.virtual_memory()
        pi = _perf_info()
        page = pi.PageSize if pi else 4096
        io = psutil.disk_io_counters()
        # network: PDH rates (~0.1 ms) instead of psutil.net_io_counters (~6 ms of CPU per call on Windows)
        rx, tx = self._pdh.array("rx"), self._pdh.array("tx")
        net = None if (rx or tx) else psutil.net_io_counters()
        # only physical adapters: a VPN, Hyper-V switch or WSL counts the same traffic again on its virtual adapter
        rx = [(i, v) for i, v in rx if not _VIRTUAL_NIC.search(i or "")] or rx
        tx = [(i, v) for i, v in tx if not _VIRTUAL_NIC.search(i or "")] or tx
        d = {"read": 0.0, "write": 0.0, "down": sum(v for _, v in rx) * 8 / 1e6, "up": sum(v for _, v in tx) * 8 / 1e6}
        if self._last_io:
            lt, lio, lnet = self._last_io
            dt = max(0.05, now - lt)
            if io and lio:  # None when no disk reports performance counters (some VMs, RAID drivers, diskperf -n)
                d["read"] = max(0, io.read_bytes - lio.read_bytes) / dt / 1e6
                d["write"] = max(0, io.write_bytes - lio.write_bytes) / dt / 1e6
            if net and lnet:
                d["down"] = max(0, net.bytes_recv - lnet.bytes_recv) / dt * 8 / 1e6
                d["up"] = max(0, net.bytes_sent - lnet.bytes_sent) / dt * 8 / 1e6
        self._last_io = (now, io, net)
        try:
            du = psutil.disk_usage("C:\\")
        except OSError:
            du = None
        bat = psutil.sensors_battery()
        b = self._battery()
        g = self._gpu(on_battery=bool(bat and not bat.power_plugged))
        out = {
            "cpu": round(cpu, 1), "threads": [round(x / 100, 3) for x in per], "cpuFreq": round(freq, 2) if freq else None,
            **self._cpu_temp(),
            "ram": round((vm.total - vm.available) / GB, 2), "ramTotal": round(vm.total / GB, 1),
            "ramCache": round(pi.SystemCache * page / GB, 2) if pi else None,
            "ramCommit": round(pi.CommitTotal * page / GB, 2) if pi else None,
            "ramCommitLimit": round(pi.CommitLimit * page / GB, 2) if pi else None,
            "procs": pi.ProcessCount if pi else None, "threadsTotal": pi.ThreadCount if pi else None,
            "diskRead": round(d["read"], 2), "diskWrite": round(d["write"], 2),
            "diskUsed": round(du.used / GB) if du else None, "diskTotal": round(du.total / GB) if du else None,
            "down": round(d["down"], 2), "up": round(d["up"], 2), "ping": self._slow["ping"], "wifiSignal": self._slow["wifiSignal"],
            "battery": round(bat.percent) if bat else None, "charging": bool(bat.power_plugged) if bat else None,
            "batterySecs": bat.secsleft if bat and bat.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else None,
            "batteryPower": b.get("power"), "batteryHealth": b.get("health"), "batteryCycles": b.get("cycles"),
            "batteryDesignWh": b.get("designWh"), "batteryFullWh": b.get("fullWh"),
            "gpu": g.get("gpu"), "gpuTemp": g.get("temp"), "gpuPower": g.get("power"), "gpuClock": g.get("clock"),
            "vram": g.get("vram"), "vramTotal": g.get("vramTotal"), "fanGpu": g.get("fan"), "fanCpu": None,
            "igpu": g.get("igpu"), "gpuState": g.get("state"),
            "uptime": int(time.time() - psutil.boot_time()), "ts": time.time(),
        }
        return fakepc.telemetry(out) if fakepc.active() else out  # developer-only: a test copy posing as a tower

    # ---------- GPU: PDH for usage/VRAM (never wakes it), NVML for sensors only while awake ----------
    def _gpu(self, on_battery):
        now = time.monotonic()
        if not self._adapters or now - self._adapters_at > 300:
            self._adapters = _adapters()
            self._adapters_at = now
        names = dict(self._adapters)
        discrete = sorted((gpu_rank(n), i, k) for i, (k, n) in enumerate(self._adapters) if gpu_kind(n) == "discrete")
        dgpu = discrete[0][2] if discrete else None
        igpu = next((k for k, n in self._adapters if gpu_kind(n) == "integrated"), None)
        per_engine = {}
        for inst, v in self._pdh.array("eng", nonzero=True):
            m = _RE_LUID.search(inst)
            if m and m.group(3):
                k = (m.group(1), m.group(2), m.group(3))
                per_engine[k] = per_engine.get(k, 0.0) + v
        util = {k: 0.0 for k, _n in self._adapters}  # an adapter with no busy engine is at 0 %
        for (hi, lo, _eng), v in per_engine.items():
            key = f"{int(hi, 16):08x}{int(lo, 16):08x}"
            util[key] = max(util.get(key, 0.0), min(100.0, v))  # Task Manager: busiest engine of the adapter
        ded = {}
        for inst, v in self._pdh.array("ded"):
            key = _luid_key(inst)
            if key:
                ded[key] = ded.get(key, 0.0) + v
        out = {"igpu": round(util[igpu], 1) if igpu in util else None}
        if dgpu is None:  # no dedicated GPU: the integrated one is "the GPU"
            out.update(gpu=out["igpu"], state="integrated" if igpu else None)
            return out
        out["gpu"] = round(util.get(dgpu, 0.0), 1) if util else None
        out["vram"] = round(ded[dgpu] / GB, 2) if dgpu in ded else None
        # power state of the dedicated GPU (cheap; refreshed every 2 s)
        if now - self._pstate_at > 2:
            self._pstate = _display_power_states()
            self._pstate_at = now
        dname = names.get(dgpu, "")
        dstate = next((s for n, s in self._pstate.items() if n == dname), None)
        if dstate is not None and dstate > 1:
            self._nv.shutdown()
            out["state"] = "asleep"  # D3: powered down, nothing to read without waking it
            out["vramTotal"] = self._nv_last.get("vramTotal")
            return out
        busy = (out["gpu"] or 0) >= 1
        if busy:
            self._dgpu_quiet_since = None
        elif self._dgpu_quiet_since is None:
            self._dgpu_quiet_since = now
        quiet_for = 0 if busy else now - (self._dgpu_quiet_since or now)
        if not self.gpu_wanted or (on_battery and quiet_for > 15):
            # on battery an idle dGPU must be left alone or it never reaches D3cold
            self._nv.shutdown()
            out["state"] = "idle"
            out["vramTotal"] = self._nv_last.get("vramTotal")
            return out
        try:
            nv = self._nv.read() if "nvidia" in dname.lower() else None
        except Exception:  # noqa: BLE001  a driver that fails a call must not take every other reading with it
            nv = None
        if nv:
            self._nv_last = nv
            out.update(temp=nv["temp"], power=nv["power"], clock=nv["clock"], fan=nv["fan"], vramTotal=nv["vramTotal"])
            if out["gpu"] is None:
                out["gpu"] = nv["gpu"]
            if out["vram"] is None:
                out["vram"] = nv["vramUsed"]
        out["state"] = "active"
        return out

    # ---------- battery (IOCTL; WMI is ~1000x more expensive) ----------
    def _battery(self):
        now = time.monotonic()
        if self._bat_path is None and now - self._bat_paths_at > 30:
            paths = _battery_paths()
            self._bat_path = paths[0] if paths else None
            self._bat_paths_at = now
        if not self._bat_path:
            return {}
        r = _battery_read(self._bat_path)
        if r is None:
            self._bat_path = None
            return {}
        out = {}
        if r["rate"] is not None:
            out["power"] = round(r["rate"] / 1000, 1)  # mW, negative while discharging
        if r["design"] and r["full"]:
            out["health"] = round(min(100, r["full"] / r["design"] * 100))
            out["designWh"], out["fullWh"] = round(r["design"] / 1000, 1), round(r["full"] / 1000, 1)
        out["cycles"] = r["cycles"] or None  # 0 = the firmware does not report it
        self._bat_static = out
        return out

    def battery_info(self):
        return dict(self._bat_static)

    # ---------- CPU temperature: ACPI thermal zones only, with the classic fake values discarded ----------
    _acpi_missing = False

    def _cpu_temp(self):
        """{cpuTemp, cpuTempSrc}: the ACPI thermal zones, else what the slow loop last read from a hardware monitor
        the user runs (engine/sensors.py)."""
        t = self._acpi_temp()
        self._acpi_missing = t is None
        if t is not None:
            return {"cpuTemp": t, "cpuTempSrc": "acpi"}
        try:
            from . import sensors
            t, src = sensors.peek()
        except Exception:
            t, src = None, None
        return {"cpuTemp": t, "cpuTempSrc": src}

    def _acpi_temp(self):
        temps = []
        for _inst, v in self._pdh.array("tz"):
            c = v / 10 - 273.15  # tenths of a kelvin
            if 35 < c < 110:  # many laptops report a fixed 27.8 °C placeholder: not a real reading
                temps.append(c)
        return round(max(temps)) if temps else None

    # ---------- slow data: every few seconds ----------
    ping_on = True  # Settings > General: "Measure latency" (a TCP connection to 1.1.1.1, nothing is sent)

    def _slow_loop(self):
        n = 0
        while not self._stop.is_set():
            if self.active:
                # lighter mode (battery): the latency every 6 s instead of 2 s and Wi-Fi every minute instead of 10 s
                if self.ping_on:
                    if not self.lite or n % 3 == 0:
                        self._ping()
                else:
                    self._slow["ping"] = None
                if n % (30 if self.lite else 5) == 0:
                    self._wifi()
                if self._acpi_missing:
                    try:  # a hardware monitor the user runs (it can take a moment, so not on the fast loop)
                        from . import sensors
                        sensors.cpu_temp(max_age=0)
                    except Exception:
                        pass
                n += 1
                self._stop.wait(2.0)
            else:
                self._wake_slow.wait(30.0)
                self._wake_slow.clear()

    def _ping(self):
        t0 = time.perf_counter()
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=1.5):
                pass
            self._slow["ping"] = round((time.perf_counter() - t0) * 1000)
        except OSError:
            self._slow["ping"] = None

    def _has_wifi(self):
        """False when Windows has no Wi-Fi adapter (the WLAN service keeps one key per adapter): then the Wi-Fi API is
        not asked every few seconds just to learn there is nothing. Checked again every minute."""
        at, val = self._wifi_probe
        if time.monotonic() - at > 60:
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WlanSvc\Interfaces") as k:
                    val = winreg.QueryInfoKey(k)[0] > 0
            except OSError:
                val = True  # unknown: ask the Wi-Fi API anyway
            self._wifi_probe = (time.monotonic(), val)
        return val

    def _wifi(self):
        if not self._has_wifi():
            self._slow["wifiSignal"] = None
            return
        try:  # in this process (engine/wlan.py): no netsh.exe every ten seconds
            from . import wlan
            self._slow["wifiSignal"] = wlan.signal()
        except Exception:
            self._slow["wifiSignal"] = None

    @staticmethod
    def _base_clock():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return int(winreg.QueryValueEx(k, "~MHz")[0])
        except OSError:
            f = psutil.cpu_freq()
            return f.max if f else None

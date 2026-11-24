"""Live Monitor: what the continuous telemetry does not bring. Processes per program (with their real icon), disks
with SMART, network (adapters, Wi-Fi, DNS, apps with connections), battery (powercfg report) and health (devices
with errors and critical events from the System log). Everything is read-only except end_process and set_dns
(the interface asks for confirmation first)."""
import ctypes
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes

import psutil

from ..i18n import tr
from .ps import ps_json, gather

EXPORTS = {"processes", "end_process", "open_location", "disks", "network", "public_ip", "dns_test", "set_dns",
           "dns_undo", "battery_report", "health", "gpu_info", "memory_info", "icons"}

NO_WINDOW = 0x08000000
MB = 1024 ** 2
GB = 1024 ** 3
_lock = threading.Lock()


def _admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ======================================================================================= processes
_desc_cache = {}     # exe -> file description
_icon_cache = {}     # exe -> data URI or None
_static = {}         # pid -> (name, exe): does not change while the process lives
ICON_BUDGET = 0.6    # max seconds spent extracting icons per call (the rest arrive in the next calls)
SYS_DIRS = (os.environ.get("SystemRoot", r"C:\Windows").lower(),)
OWN = os.getpid()


def _file_desc(path):
    """FileDescription of the .exe (what Task Manager shows)."""
    if path in _desc_cache:
        return _desc_cache[path]
    desc = None
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            ver.GetFileVersionInfoW(path, 0, size, buf)
            p = ctypes.c_void_p()
            ln = wintypes.UINT()
            if ver.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(p), ctypes.byref(ln)) and ln.value >= 4:
                lang, cp = ctypes.cast(p, ctypes.POINTER(ctypes.c_ushort * 2)).contents
                for key in (f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription", "\\StringFileInfo\\040904b0\\FileDescription"):
                    if ver.VerQueryValueW(buf, key, ctypes.byref(p), ctypes.byref(ln)) and ln.value > 1:
                        desc = ctypes.wstring_at(p, ln.value - 1).strip()
                        if desc:
                            break
    except Exception:
        desc = None
    _desc_cache[path] = desc
    return desc


def _icon(exe):
    if exe not in _icon_cache:
        try:
            from .removal.icons import icon_for_file
            _icon_cache[exe] = icon_for_file(exe) if exe and os.path.isfile(exe) else None
        except Exception:
            _icon_cache[exe] = None
    return _icon_cache[exe]


# Parts of Windows with no program file: they get the Windows logo
PSEUDO = {"system", "registry", "memcompression", "memory compression", "idle", "system idle process", "secure system",
          "interrupts", "vmmem", "vmmemwsl", "vmmemws"}
SERVICE_HOSTS = {"svchost.exe", "services.exe", "wudfhost.exe", "dllhost.exe"}
_svc = {"at": 0, "paths": set(), "byname": {}}


def _services():
    """Program files of the installed services (refreshed every 10 min): which processes are services, and the path
    of protected ones Windows will not tell a normal user (an antivirus, for example)."""
    if time.monotonic() - _svc["at"] > 600 or not _svc["at"]:
        paths, byname = set(), {}
        try:
            from .removal.programs import exe_from_command
            for sv in psutil.win_service_iter():
                try:
                    exe = exe_from_command(sv.binpath())
                except (psutil.Error, OSError):
                    continue
                if exe and os.path.isabs(exe):
                    paths.add(exe.lower())
                    byname.setdefault(os.path.basename(exe).lower(), exe)
        except Exception:
            pass
        _svc.update(at=time.monotonic(), paths=paths, byname=byname)
    return _svc


def _find_exe(name):
    """Where a process lives when Windows refuses to say (protected and system processes, normal mode)."""
    name = (name or "").lower()
    if not name.endswith(".exe"):
        return ""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for d in (os.path.join(root, "System32"), root, os.path.join(root, "SysWOW64")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    p = _services()["byname"].get(name)
    return p if p and os.path.isfile(p) else ""


def _proc_icon(name, exe, system):
    """Icon for a process row: its own icon; the Windows logo for parts of Windows; a gear for services
    without an icon; the generic program icon otherwise (like Explorer). Never None if Windows can draw one."""
    try:
        from .removal import icons
    except Exception:
        return None
    low = (name or "").lower()
    if os.path.splitext(low)[0] in PSEUDO or low in PSEUDO:
        return icons.windows_icon()
    if low in SERVICE_HOSTS:
        return icons.stock_icon("settings")
    path = exe if exe and os.path.isfile(exe) else _find_exe(name)
    own = _icon(path) if path else None
    if own:
        return own
    if path and path.lower() in _services()["paths"]:
        return icons.stock_icon("settings")
    in_windows = path and path.lower().startswith(SYS_DIRS) and "\\driverstore\\" not in path.lower()  # not drivers' helpers
    if in_windows or (not path and system):
        return icons.windows_icon()
    return icons.type_icon(".exe")


class _PdhArray:
    r"""Reads multi-instance counters (\Process(*)\..., \GPU Engine(*)\...) in ONE single query.
    The query stays open between calls: rate counters are computed against the previous reading."""

    class _VAL(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("dbl", ctypes.c_double)]

    def __init__(self, paths):
        self.ok = False
        self.c = {}
        try:
            self.pdh = ctypes.windll.pdh
            self.q = wintypes.HANDLE()
            if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) != 0:
                return
            for k, path in paths.items():
                h = wintypes.HANDLE()
                if self.pdh.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h)) == 0:
                    self.c[k] = h
            self.pdh.PdhCollectQueryData(self.q)
            self.ok = bool(self.c)
        except Exception:
            pass

    def collect(self):
        return self.ok and self.pdh.PdhCollectQueryData(self.q) == 0

    def healthy(self):
        """Usable for the processes list: the query opened and the counter that names every process is there."""
        return self.ok and "pid" in self.c

    def close(self):
        try:
            if self.ok:
                self.pdh.PdhCloseQuery(self.q)
        except Exception:
            pass
        self.ok = False

    def array(self, key):
        """List of (instance, value) for the counter."""
        h = self.c.get(key)
        if h is None:
            return []

        class ITEM(ctypes.Structure):
            _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _PdhArray._VAL)]
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        flags = 0x200 | 0x8000  # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        self.pdh.PdhGetFormattedCounterArrayW(h, flags, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            return []
        buf = (ctypes.c_byte * size.value)()
        if self.pdh.PdhGetFormattedCounterArrayW(h, flags, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(ITEM))
        return [(items[i].szName or "", items[i].FmtValue.dbl) for i in range(count.value)]


_pdh = None
_pdh_next = 0.0      # monotonic time before which a failed PDH setup is not tried again
PDH_RETRY = 60       # seconds between attempts to set the counters up again after they failed
_ps_io = {}          # fallback: pid -> (bytes read+written, time)
PDH_PATHS = {"pid": r"\Process(*)\ID Process", "cpu": r"\Process(*)\% Processor Time",
             "ram": r"\Process(*)\Working Set - Private", "io": r"\Process(*)\IO Data Bytes/sec",
             "gpu": r"\GPU Engine(*)\Utilization Percentage"}


def _read_pdh(pdh):
    """(inst, vals, gpu) from the performance counters, or None when they gave nothing usable (corrupted
    counters, the "ID Process" counter missing...)."""
    if not pdh.collect():
        return None
    # NOTE: processes with the same name (svchost, chrome...) come with the SAME instance name, without "#n".
    # The lists of all counters come in the same order: they are paired by position, not by name.
    pid_arr = pdh.array("pid")
    if not pid_arr:
        return None
    inst = [(name, int(v)) for name, v in pid_arr]
    vals = {}
    for k in ("cpu", "ram", "io"):
        arr = pdh.array(k)
        if len(arr) == len(pid_arr) and all(a[0] == b[0] for a, b in zip(arr, pid_arr)):
            vals[k] = {pid: arr[i][1] for i, (_, pid) in enumerate(inst)}
        else:  # different order (rare): only the ones with a unique name
            uniq = {}
            for n, pid in inst:
                uniq[n] = None if n in uniq else pid
            vals[k] = {uniq[n]: v for n, v in arr if uniq.get(n)}
    inst = [(n, pid) for n, pid in inst if pid > 0]
    gpu = {}
    for name, v in pdh.array("gpu"):
        m = re.match(r"pid_(\d+)_", name)
        if m:
            pid = int(m.group(1))
            gpu[pid] = max(gpu.get(pid, 0.0), v)
    return inst, vals, gpu


def _read_psutil():
    """Fallback when the performance counters don't work: the same (inst, vals, gpu) from psutil. CPU is % of one
    core (the first reading of a process is 0: psutil keeps the Process objects between calls), RAM the process'
    private bytes, disk the read+write rate since the previous call. No GPU per process. The attributes are read
    together (one system query per process instead of three)."""
    now = time.monotonic()
    inst, cpu, ram, io, alive = [], {}, {}, {}, set()
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "io_counters"]):
        i = p.info
        pid = i["pid"]
        if not pid:
            continue
        alive.add(pid)
        inst.append((os.path.splitext(i["name"] or "")[0], pid))
        cpu[pid] = float(i["cpu_percent"] or 0.0)
        mem = i["memory_info"]
        ram[pid] = float(getattr(mem, "private", None) or getattr(mem, "rss", 0) or 0) if mem else 0.0
        c = i["io_counters"]
        io[pid] = 0.0
        if c is not None:
            total = c.read_bytes + c.write_bytes
            prev = _ps_io.get(pid)
            if prev:
                io[pid] = max(0.0, (total - prev[0]) / max(0.001, now - prev[1]))
            _ps_io[pid] = (total, now)
    for pid in list(_ps_io):
        if pid not in alive:
            _ps_io.pop(pid, None)
    return inst, {"cpu": cpu, "ram": ram, "io": io}, {}


def processes(known=None):
    """Processes grouped by program (like Task Manager), read in one go with Windows performance counters:
    CPU in % of the total, private RAM in MB, disk in MB/s, GPU in %, network = open connections.
    If the counters don't work (corrupted performance counters) it falls back to psutil (no GPU per process)
    and tries to set the counters up again every PDH_RETRY seconds.
    The icon is only sent for programs the interface does not know yet (`known`)."""
    global _pdh, _pdh_next
    known = set(known or [])
    with _lock:
        data = None
        if _pdh is None and time.monotonic() >= _pdh_next:
            _pdh = _PdhArray(PDH_PATHS)
            if _pdh.healthy():
                time.sleep(0.25)
            else:
                _pdh.close()
                _pdh, _pdh_next = None, time.monotonic() + PDH_RETRY
        if _pdh is not None:
            data = _read_pdh(_pdh)
            if data is None:  # it stopped working: drop it and retry later (never cache a failed query forever)
                _pdh.close()
                _pdh, _pdh_next = None, time.monotonic() + PDH_RETRY
        gpu_pp = data is not None and _pdh is not None and "gpu" in _pdh.c
        if data is None:
            try:
                data = _read_psutil()
            except Exception:
                return {"list": [], "total": 0, "gpuPerProcess": False, "admin": _admin(), "error": "counters not available"}
        ncpu = psutil.cpu_count() or 1
        inst, vals, gpu = data
        conns = {}
        try:
            for c in psutil.net_connections("inet"):
                if c.pid and c.status == psutil.CONN_ESTABLISHED:
                    conns[c.pid] = conns.get(c.pid, 0) + 1
        except (psutil.AccessDenied, OSError):
            pass
        t0 = time.monotonic()
        groups = {}
        alive = {pid for _, pid in inst}
        for iname, pid in inst:
            st = _static.get(pid)
            if st is None:
                exe = ""
                try:
                    exe = psutil.Process(pid).exe()
                except (psutil.Error, OSError):
                    pass
                base = iname.split("#")[0]
                st = _static[pid] = (os.path.basename(exe) if exe else base + ".exe", exe)
            name, exe = st
            key = name.lower()
            g = groups.get(key)
            if g is None:
                system = (not exe) or exe.lower().startswith(SYS_DIRS)
                where = exe or _find_exe(name)  # protected processes: only for the description and the icon
                desc = _file_desc(where) if where and (where in _desc_cache or time.monotonic() - t0 < 1.0) else None
                g = groups[key] = {"key": key, "n": desc or os.path.splitext(name)[0], "exe": name, "path": exe, "sys": bool(system),
                                   "k": 0, "pids": [], "cpu": 0.0, "ram": 0.0, "gpu": 0.0, "disk": 0.0, "net": 0}
            g["k"] += 1
            g["pids"].append(pid)
            g["cpu"] += vals["cpu"].get(pid, 0.0) / ncpu
            g["ram"] += vals["ram"].get(pid, 0.0) / MB
            g["disk"] += vals["io"].get(pid, 0.0) / MB
            g["gpu"] = max(g["gpu"], min(100.0, gpu.get(pid, 0.0)))
            g["net"] += conns.get(pid, 0)
        for pid in list(_static):
            if pid not in alive:
                _static.pop(pid, None)
        out = []
        t_icons = time.monotonic()
        for g in sorted(groups.values(), key=lambda g: -g["ram"]):
            g["cpu"], g["ram"], g["gpu"], g["disk"] = round(min(100, g["cpu"]), 2), round(g["ram"], 1), round(g["gpu"], 1), round(g["disk"], 2)
            if g["key"] not in known:
                if not g["path"] or g["path"] in _icon_cache or time.monotonic() - t_icons < ICON_BUDGET:
                    g["icon"] = _proc_icon(g["exe"], g["path"], g["sys"])  # no time left: not sent, the interface asks again
            if OWN in g["pids"]:
                g["own"] = True
            out.append(g)
        return {"list": out, "total": len(alive), "gpuPerProcess": gpu_pp, "admin": _admin()}


def icons(specs, budget=1.5):
    """Shared icon lookup for lists in any module (Storage's files, for example). Each spec is 'ext:.mp4' (file type),
    'file:<path>' (the file's own icon, or its type's), 'dir:' (folder), 'stock:<name>' or 'windows'.
    Returns {spec: data URI}; what does not fit in the time budget is left out and arrives on the next call."""
    from .removal import icons as ic
    out, t0 = {}, time.monotonic()
    for spec in list(specs or [])[:400]:
        if time.monotonic() - t0 > budget:
            break
        kind, _, val = str(spec).partition(":")
        try:
            uri = (ic.type_icon(val) if kind == "ext" else ic.file_icon(val) if kind == "file" else
                   ic.type_icon("", folder=True) if kind == "dir" else ic.stock_icon(val) if kind == "stock" else
                   ic.windows_icon() if kind == "windows" else None)
        except Exception:
            uri = None
        if uri:
            out[spec] = uri
    return out


def end_process(key):
    """Ends every process of a program. Refuses with Windows processes and with RsHub itself."""
    key = str(key).lower()
    killed, denied = 0, 0
    for p in psutil.process_iter(["pid", "name", "exe"]):
        if (p.info["name"] or "").lower() != key or p.info["pid"] == OWN:
            continue
        exe = (p.info["exe"] or "").lower()
        if not exe or exe.startswith(SYS_DIRS):
            denied += 1
            continue
        try:
            p.terminate()
            killed += 1
        except psutil.Error:
            denied += 1
    return {"killed": killed, "denied": denied}


def open_location(path):
    if path and os.path.exists(path):
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)], creationflags=NO_WINDOW)
        return True
    return False


# ======================================================================================= memory and GPU
def memory_info():
    class PI(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                    ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t), ("PhysicalAvailable", ctypes.c_size_t),
                    ("SystemCache", ctypes.c_size_t), ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                    ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t), ("HandleCount", wintypes.DWORD),
                    ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]
    pi = PI()
    pi.cb = ctypes.sizeof(pi)
    ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb)
    pg = pi.PageSize
    comp = None
    for p in psutil.process_iter(["name"]):
        if p.info["name"] == "MemCompression":
            try:
                comp = round(p.memory_info().rss / MB)
            except psutil.Error:
                pass
            break
    return {"paged": round(pi.KernelPaged * pg / MB), "nonpaged": round(pi.KernelNonpaged * pg / MB),
            "commitLimit": round(pi.CommitLimit * pg / GB, 1), "commitPeak": round(pi.CommitPeak * pg / GB, 1),
            "handles": pi.HandleCount, "threads": pi.ThreadCount, "processes": pi.ProcessCount, "compressed": comp}


_gpu_info_cache = {}


def gpu_info():
    """Spec sheet of the NVIDIA GPU (if there is one) through NVML (in-process, ~40 ms; nvidia-smi was ~120 ms + a process).
    If the dedicated GPU is powered down (D3) it is NOT woken up just for this: the last sheet read is returned with
    asleep=True, or {"asleep": True} if there is none yet."""
    from .telemetry import _Nvml, _display_power_states
    states = _display_power_states()
    nv_state = next((s for n, s in states.items() if "nvidia" in n.lower()), None)
    if nv_state is None and states:
        return None  # no NVIDIA adapter
    if nv_state is not None and nv_state > 1:
        return dict(_gpu_info_cache, asleep=True) if _gpu_info_cache else {"asleep": True}
    nv = _Nvml()
    if not nv.init():
        return None
    L, d = nv.lib, nv.dev
    try:
        ok = lambda rc: rc == 0
        name, drv = ctypes.create_string_buffer(96), ctypes.create_string_buffer(80)
        L.nvmlDeviceGetName(d, name, 96)
        L.nvmlSystemGetDriverVersion(drv, 80)
        mem = (ctypes.c_ulonglong * 3)()
        plim, mclk, gen, width, slow, temp = (ctypes.c_uint() for _ in range(6))
        out = {"name": name.value.decode(errors="replace"), "driver": drv.value.decode(errors="replace"),
               "vram": round(mem[0] / GB) if ok(L.nvmlDeviceGetMemoryInfo(d, mem)) else None,
               "powerLimit": round(plim.value / 1000) if ok(L.nvmlDeviceGetPowerManagementLimit(d, ctypes.byref(plim))) else None,
               "maxClock": round(mclk.value / 1000, 2) if ok(L.nvmlDeviceGetMaxClockInfo(d, 0, ctypes.byref(mclk))) else None,
               "pcie": None, "tlimitMargin": None, "asleep": False}
        if ok(L.nvmlDeviceGetMaxPcieLinkGeneration(d, ctypes.byref(gen))) and ok(L.nvmlDeviceGetMaxPcieLinkWidth(d, ctypes.byref(width))):
            out["pcie"] = f"PCIe {gen.value}.0 ×{width.value}"
        # nvidia-smi temperature.gpu.tlimit: degrees left before the GPU slows itself down. Newer drivers expose it as
        # nvmlDeviceGetMarginTemperature; older ones only have the "GPU max" threshold (3), which gives the same margin.
        class _Margin(ctypes.Structure):
            _fields_ = [("version", ctypes.c_uint), ("margin", ctypes.c_int)]
        mg = _Margin(ctypes.sizeof(_Margin) | (1 << 24), 0)
        try:
            if ok(L.nvmlDeviceGetMarginTemperature(d, ctypes.byref(mg))):
                out["tlimitMargin"] = mg.margin
        except AttributeError:
            pass
        if out["tlimitMargin"] is None and ok(L.nvmlDeviceGetTemperatureThreshold(d, 3, ctypes.byref(slow))) \
                and ok(L.nvmlDeviceGetTemperature(d, 0, ctypes.byref(temp))):
            out["tlimitMargin"] = int(slow.value) - int(temp.value)
        _gpu_info_cache.clear()
        _gpu_info_cache.update({k: v for k, v in out.items() if k != "tlimitMargin"})
        return out
    finally:
        nv.shutdown()


# ======================================================================================= disks
def disks():
    """Physical disks (with SMART when running as administrator) and partitions with their space."""
    def get_phys():
        return ps_json(_PHYS_PS, timeout=40, as_list=True)

    def get_vols():
        return ps_json(_VOLS_PS, timeout=30, as_list=True)

    def get_trim():
        try:
            out = subprocess.run(["fsutil", "behavior", "query", "DisableDeleteNotify"], capture_output=True, timeout=5,
                                 creationflags=NO_WINDOW).stdout.decode("cp850", "replace")
            m = re.search(r"NTFS\s+DisableDeleteNotify\s*=\s*(\d)", out)
            return (m.group(1) == "0") if m else None
        except Exception:
            return None
    phys, vols, trim = gather(get_phys, get_vols, get_trim)  # three independent reads, at the same time
    return {"physical": phys, "volumes": vols, "admin": _admin(), "trim": trim}


_PHYS_PS = r"""
Get-PhysicalDisk | ForEach-Object {
  $r = $null; try { $r = $_ | Get-StorageReliabilityCounter -ErrorAction Stop } catch {}
  [pscustomobject]@{ id=$_.DeviceId; name=$_.FriendlyName; media="$($_.MediaType)"; bus="$($_.BusType)"; size=$_.Size; health="$($_.HealthStatus)"
    op="$($_.OperationalStatus)"; fw=$_.FirmwareVersion; spindle=$_.SpindleSpeed
    temp= if ($r) { $r.Temperature } else { $null }; tempMax= if ($r) { $r.TemperatureMax } else { $null }
    wear= if ($r) { $r.Wear } else { $null }; hours= if ($r) { $r.PowerOnHours } else { $null }; starts= if ($r) { $r.StartStopCycleCount } else { $null }
    readErr= if ($r) { $r.ReadErrorsTotal } else { $null }; writeErr= if ($r) { $r.WriteErrorsTotal } else { $null }; smart=[bool]$r }
}"""
_VOLS_PS = r"""
Get-Partition | Where-Object { $_.Type -ne 'Reserved' } | ForEach-Object { $v = $null; try { $v = $_ | Get-Volume -ErrorAction Stop } catch {}
  [pscustomobject]@{ disk=$_.DiskNumber; letter="$($_.DriveLetter)".Trim(); type="$($_.Type)"; size=$_.Size; hidden=[bool]$_.IsHidden -or -not $_.DriveLetter
    label= if ($v) { $v.FileSystemLabel } else { '' }; fs= if ($v) { $v.FileSystem } else { '' }; free= if ($v) { $v.SizeRemaining } else { $null } } }"""


# ======================================================================================= network
def _wifi():
    """The Wi-Fi connection from Windows' Wi-Fi API in this process (engine/wlan.py; it was `netsh wlan show
    interfaces`, parsed per language), or None when Windows can't say."""
    try:
        from . import wlan
        return wlan.current()
    except Exception:
        return None


_NET_PS = r"""
Get-NetIPConfiguration | Where-Object { $_.NetAdapter.Status -eq 'Up' } | ForEach-Object {
  [pscustomobject]@{ alias=$_.InterfaceAlias; index=$_.InterfaceIndex; desc=$_.InterfaceDescription
    ipv4=@($_.IPv4Address | ForEach-Object { $_.IPAddress }); gw=@($_.IPv4DefaultGateway | ForEach-Object { $_.NextHop })
    dns=@($_.DNSServer | Where-Object { $_.AddressFamily -eq 2 } | ForEach-Object { $_.ServerAddresses })
    dhcpDns=[bool]((Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$($_.NetAdapter.InterfaceGuid)" -ErrorAction SilentlyContinue).NameServer -eq '')
    mac=$_.NetAdapter.MacAddress; speed=$_.NetAdapter.LinkSpeed; media="$($_.NetAdapter.PhysicalMediaType)" } }"""


def _dns_from_dhcp(guid):
    """Whether the adapter's IPv4 DNS comes from DHCP: its NameServer value exists and is empty (as _NET_PS reads it;
    a missing key or value is False there too)."""
    if not guid:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces" + "\\" + str(guid)) as k:
            return winreg.QueryValueEx(k, "NameServer")[0] == ""
    except OSError:
        return False


def _mac(raw):
    """"EC3A567C5378" as Get-NetAdapter's MacAddress shows it: "EC-3A-56-7C-53-78"."""
    s = re.sub(r"[^0-9A-Fa-f]", "", str(raw or "")).upper()
    return "-".join(s[i:i + 2] for i in range(0, len(s), 2)) if len(s) == 12 else (str(raw) if raw else "")


def _net_wmi():
    """The rows _NET_PS returns, from WMI inside this process: Get-NetIPConfiguration alone took ~3 s (it loads the
    NetTCPIP module); these four queries take ~0.1 s. The adapters are the visible ones that are up (Get-NetAdapter
    leaves the hidden ones out, and so does this class)."""
    from . import wmi
    from .system import _PHYS, _link_speed
    ns = r"root\StandardCimv2"
    ads = wmi.query(ns, "MSFT_NetAdapter", ["Name", "InterfaceDescription", "InterfaceIndex", "InterfaceOperationalStatus",
                                            "Hidden", "DeviceID", "NetworkAddresses", "PermanentAddress", "Speed",
                                            "ReceiveLinkSpeed", "TransmitLinkSpeed", "NdisPhysicalMedium"])
    ads = [a for a in ads if a.get("InterfaceOperationalStatus") == 1 and not a.get("Hidden")]
    if not ads:
        return []

    def active(r):  # the ActiveStore (what Get-NetIPConfiguration shows), not the persistent one
        return r.get("Store") in (None, 1)

    def add(d, k, v):
        lst = d.setdefault(k, [])
        if v and v not in lst:
            lst.append(v)
    ips, gws, dns = {}, {}, {}
    for r in wmi.query(ns, "MSFT_NetIPAddress", ["InterfaceIndex", "IPAddress", "Store"], "AddressFamily=2"):
        if active(r):
            add(ips, r.get("InterfaceIndex"), r.get("IPAddress"))
    for r in wmi.query(ns, "MSFT_NetRoute", ["InterfaceIndex", "NextHop", "Store"], "DestinationPrefix='0.0.0.0/0'"):
        if active(r):
            add(gws, r.get("InterfaceIndex"), r.get("NextHop"))
    for r in wmi.query(ns, "MSFT_DNSClientServerAddress", ["InterfaceIndex", "ServerAddresses"], "AddressFamily=2"):
        for x in (r.get("ServerAddresses") or []):
            add(dns, r.get("InterfaceIndex"), str(x) if x else None)
    out = []
    for a in ads:
        i = a.get("InterfaceIndex")
        net = a.get("NetworkAddresses")
        speed = a.get("Speed") or max(a.get("ReceiveLinkSpeed") or 0, a.get("TransmitLinkSpeed") or 0)
        out.append({"alias": a.get("Name"), "index": i, "desc": a.get("InterfaceDescription"), "ipv4": ips.get(i, []),
                    "gw": gws.get(i, []), "dns": dns.get(i, []), "dhcpDns": _dns_from_dhcp(a.get("DeviceID")),
                    "mac": _mac((net[0] if isinstance(net, list) and net else net) or a.get("PermanentAddress")),
                    "speed": _link_speed(speed),
                    "media": _PHYS.get(a.get("NdisPhysicalMedium"), str(a.get("NdisPhysicalMedium") or ""))})
    return out


def _net_adapters():
    try:
        return _net_wmi()
    except Exception as e:  # no pythonnet, a broken WMI repository...: the PowerShell script gives the same rows
        print("[monitor] WMI failed, using PowerShell:", e, flush=True)
    return ps_json(_NET_PS, timeout=30, as_list=True)


def _established_by_pid():
    apps = {}
    try:
        for c in psutil.net_connections("inet"):
            if c.pid and c.status == psutil.CONN_ESTABLISHED:
                apps[c.pid] = apps.get(c.pid, 0) + 1
    except (psutil.AccessDenied, OSError):
        pass
    return apps


def network():
    # the adapters, the Wi-Fi API and the connection table do not depend on each other: all at once
    cfg, wifi, apps = gather(_net_adapters, _wifi, _established_by_pid)
    byname = {}
    for pid, n in apps.items():
        try:
            p = psutil.Process(pid)
            name = p.name()
        except psutil.Error:
            continue
        try:
            exe = p.exe()
        except psutil.Error:  # protected (an antivirus, for example): still listed
            exe = ""
        where = exe or _find_exe(name)
        g = byname.setdefault(name.lower(), {"n": (_file_desc(where) if where else None) or os.path.splitext(name)[0], "exe": name, "conns": 0, "icon": None})
        g["conns"] += n
        if g["icon"] is None:
            g["icon"] = _proc_icon(name, exe, not exe or exe.lower().startswith(SYS_DIRS))
    top = sorted(byname.values(), key=lambda g: -g["conns"])[:8]
    for a in cfg:
        a["virtual"] = bool(re.search(r"hyper-v|virtualbox|vmware|wsl|tap-|wintun|loopback|virtual|vpn|zerotier|tailscale", (a.get("desc") or "") + " " + (a.get("alias") or ""), re.I))
        a["gw"] = [g for g in (a.get("gw") or []) if g]
    cfg.sort(key=lambda a: (a["virtual"], not a["gw"]))
    return {"adapters": cfg, "wifi": wifi, "apps": top, "admin": _admin()}


def public_ip():
    """Only called when the user clicks "show"."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return None


def _dns_query_ms(server, name="www.microsoft.com"):
    q = os.urandom(2) + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        t0 = time.perf_counter()
        s.sendto(q, (server, 53))
        s.recvfrom(512)
        return round((time.perf_counter() - t0) * 1000, 1)
    except OSError:
        return None
    finally:
        s.close()


def dns_test(router=None):
    out = {}
    for k, ip in (("router", router), ("cloudflare", "1.1.1.1"), ("google", "8.8.8.8")):
        if ip:
            vals = [x for x in (_dns_query_ms(ip) for _ in range(3)) if x is not None]
            out[k] = min(vals) if vals else None
    return out


DNS = {"cloudflare": ("1.1.1.1", "1.0.0.1"), "google": ("8.8.8.8", "8.8.4.4")}
DNS_BACKUPS = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "RsHub", "monitor", "dns")


def _current_dns(alias):
    """(dhcp, [servers]) of the adapter's IPv4 DNS, or None when it can't be read."""
    row = ps_json("Get-DnsClientServerAddress -InterfaceAlias '%s' -AddressFamily IPv4 | Select-Object -ExpandProperty ServerAddresses"
                  % alias.replace("'", "''"), timeout=20, as_list=True)
    servers = [str(x) for x in (row or []) if x]
    # Whether the DNS (not the address) comes from DHCP: the adapter's NameServer value is empty then, and holds the
    # servers typed in by hand otherwise (network() reads it the same way). Get-NetIPInterface's Dhcp is about the IP
    # address: an adapter with an automatic address and DNS set by hand was saved as "automatic", so undoing a DNS
    # change, or choosing "automatic", left the user's own DNS servers lost.
    ns = ps_json("$a = Get-NetAdapter -InterfaceAlias '%s' -ErrorAction Stop; [string](Get-ItemProperty -ErrorAction Stop "
                 "('HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces\\' + $a.InterfaceGuid)).NameServer"
                 % alias.replace("'", "''"), timeout=20)
    if ns is None:
        return None
    return not str(ns).strip(), servers


def _dns_cmds(alias, dhcp, servers):
    if dhcp or not servers:
        return [["netsh", "interface", "ipv4", "set", "dnsservers", f"name={alias}", "source=dhcp"]]
    cmds = [["netsh", "interface", "ipv4", "set", "dnsservers", f"name={alias}", "source=static", f"address={servers[0]}", "validate=no"]]
    cmds += [["netsh", "interface", "ipv4", "add", "dnsservers", f"name={alias}", f"address={s}", f"index={i}", "validate=no"]
             for i, s in enumerate(servers[1:], 2)]
    return cmds


def _save_dns(alias, dhcp, servers):
    """Saves the previous DNS of the adapter before it changes. Raises OSError if it can't be written."""
    from .optimizer import new_bid, write_json_atomic
    bid = new_bid()
    write_json_atomic(os.path.join(DNS_BACKUPS, bid + ".json"),
                      {"t": time.time(), "alias": alias, "dhcp": dhcp, "servers": servers})
    return bid


def _run_netsh(cmds):
    """Runs the netsh commands in order and flushes the DNS cache. Returns None, or the error of the first one that
    failed (netsh's own text, in this Windows' console code page) and does not run the rest."""
    from .ps import _decode
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, timeout=15, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return tr("Windows took too long to answer. Try again.")
        except (OSError, subprocess.SubprocessError):
            return tr("netsh failed.")
        if r.returncode != 0:
            return _decode(r.stdout or b"", "utf-8").strip() or tr("netsh failed.")
    try:
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass   # the change is made; the old answers expire from the cache on their own
    return None


def dns_undo(bid):
    """Puts back the DNS an earlier set_dns replaced. Needs administrator."""
    if not _admin():
        return {"needAdmin": True}
    import json
    with open(os.path.join(DNS_BACKUPS, os.path.basename(str(bid)) + ".json"), encoding="utf-8") as fh:
        d = json.load(fh)
    err = _run_netsh(_dns_cmds(d["alias"], d["dhcp"], d["servers"]))
    return {"ok": False, "error": err} if err else {"ok": True}


def set_dns(alias, mode):
    """Changes the adapter's IPv4 DNS: 'auto' (the router's, DHCP), 'cloudflare' or 'google'. Needs administrator.
    The previous DNS is saved first (and nothing changes if that fails); the result carries `backup`, the id
    `dns_undo` takes to put it back."""
    if not _admin():
        return {"needAdmin": True}
    alias = str(alias)
    if mode not in ("auto",) + tuple(DNS):
        return {"ok": False, "error": tr("Unknown DNS mode.")}
    cur = _current_dns(alias)
    if cur is None:
        return {"ok": False, "error": tr("Not changed: the current DNS couldn't be read to save it first.")}
    if mode == "auto" and cur[0]:
        return {"ok": True, "backup": None}  # already automatic
    try:
        backup = _save_dns(alias, cur[0], cur[1])
    except OSError as e:
        return {"ok": False, "error": tr("Not changed: the previous DNS couldn't be saved ({error}).", error=e)}
    if mode == "auto":
        cmds = [["netsh", "interface", "ipv4", "set", "dnsservers", f"name={alias}", "source=dhcp"]]
    elif mode in DNS:
        a, b = DNS[mode]
        cmds = [["netsh", "interface", "ipv4", "set", "dnsservers", f"name={alias}", "source=static", f"address={a}", "validate=no"],
                ["netsh", "interface", "ipv4", "add", "dnsservers", f"name={alias}", f"address={b}", "index=2", "validate=no"]]
    else:
        return {"ok": False, "error": tr("Unknown DNS mode.")}
    err = _run_netsh(cmds)
    # the backup also goes with an error: a first command that went through (the first server set) already changed
    # the adapter, and undo is how the user gets their own DNS back
    return {"ok": False, "error": err, "backup": backup} if err else {"ok": True, "backup": backup}


# ======================================================================================= battery
_bat = {"t": 0, "data": None}


def battery_report():
    """powercfg report: capacities, cycles and usage over the last 24 h (charge level and whether it was plugged in)."""
    if _bat["data"] and time.time() - _bat["t"] < 120:
        return _bat["data"]
    if psutil.sensors_battery() is None:
        return None
    path = os.path.join(tempfile.gettempdir(), f"sh_battery_{os.getpid()}.xml")
    try:
        subprocess.run(["powercfg", "/batteryreport", "/xml", "/output", path], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        root = ET.parse(path).getroot()
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    ns = {"b": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    f = (lambda e, tag: e.find(f"b:{tag}", ns)) if ns else (lambda e, tag: e.find(tag))
    txt = lambda e: e.text.strip() if e is not None and e.text else None
    bat = root.find(".//b:Battery", ns) if ns else root.find(".//Battery")
    info = {}
    if bat is not None:
        for tag in ("Id", "Manufacturer", "Chemistry", "DesignCapacity", "FullChargeCapacity", "CycleCount"):
            info[tag] = txt(f(bat, tag))
    usage = []
    cut = time.time() - 24 * 3600
    for e in (root.findall(".//b:RecentUsage/b:UsageEntry", ns) if ns else root.findall(".//RecentUsage/UsageEntry")):
        ts = e.get("LocalTimestamp") or e.get("Timestamp")
        try:
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError):
            continue
        cap, full = e.get("ChargeCapacity"), e.get("FullChargeCapacity")
        if t < cut or not cap or not full or int(full) == 0:
            continue
        usage.append({"t": t, "pct": round(int(cap) / int(full) * 100, 1), "ac": e.get("Ac") == "1", "state": e.get("EntryType")})
    num = lambda s: int(s) if s and s.isdigit() else None
    data = {"name": info.get("Id"), "maker": info.get("Manufacturer"), "chemistry": info.get("Chemistry"),
            "design": num(info.get("DesignCapacity")), "full": num(info.get("FullChargeCapacity")),
            "cycles": num(info.get("CycleCount")), "usage": usage}
    _bat.update(t=time.time(), data=data)
    return data


# ======================================================================================= health
# device problem codes and System log events explained in plain words (English keys; translated with tr() when sent)
CM_CODES = {1: "Not configured correctly", 3: "The driver is damaged or the system is low on memory", 10: "Could not start",
            12: "Not enough free resources", 14: "Needs a restart", 18: "The driver needs to be reinstalled",
            19: "Registry configuration is damaged", 22: "Disabled", 24: "Not present or not working properly",
            28: "No driver installed", 31: "Windows could not load the driver",
            43: "Windows stopped it because it reported a problem", 45: "Disconnected", 52: "The driver signature is not valid"}
EVENT_HELP = {
    ("Microsoft-Windows-Kernel-Power", 41): "The PC turned off without shutting down properly: a power cut, a drained battery or a held power button.",
    ("EventLog", 6008): "Unexpected shutdown recorded at the next startup.",
    ("Microsoft-Windows-WER-SystemErrorReporting", 1001): "There was a blue screen. The code tells what caused it.",
    ("Display", 4101): "The display driver stopped responding and recovered. If it keeps happening, update it.",
    ("Microsoft-Windows-WHEA-Logger", 17): "Corrected hardware error (PCI Express). Only a concern if it shows up often.",
    ("Microsoft-Windows-WHEA-Logger", 19): "Corrected hardware error in the processor.",
    ("Microsoft-Windows-WHEA-Logger", 47): "Corrected memory error.",
    ("disk", 7): "The disk has a bad block. It is worth checking its health.",
    ("disk", 153): "A disk operation had to be retried.",
    ("Ntfs", 55): "The file system structure is damaged: running chkdsk is recommended.",
    ("Service Control Manager", 7000): "A service could not start.",
    ("Service Control Manager", 7001): "A service did not start because it depends on another one that failed.",
    ("Service Control Manager", 7023): "A service stopped with an error.",
    ("Service Control Manager", 7031): "A service closed unexpectedly and Windows restarted it.",
    ("Service Control Manager", 7034): "A service closed unexpectedly.",
    ("Microsoft-Windows-DistributedCOM", 10016): "Internal Windows permissions notice. It is common and does not affect use.",
    ("Microsoft-Windows-DistributedCOM", 10010): "A Windows component was slow to respond. Usually harmless.",
    ("volmgr", 161): "The memory dump could not be created when the system failed.",
    ("Microsoft-Windows-Kernel-Boot", 29): "Windows could not start on the first attempt.",
    ("Microsoft-Windows-WindowsUpdateClient", 20): "A Windows update could not be installed.",
    ("Microsoft-Windows-Time-Service", 129): "The clock could not be synced with the internet.",
    ("Netwtw10", 5002): "Wi-Fi dropped for a moment.",
    ("rtwlane", 5002): "Wi-Fi dropped for a moment.",
}


def health():
    r = ps_json(r"""
$err = @(Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode<>0' -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ name=$_.Name; code=$_.ConfigManagerErrorCode; cls=$_.PNPClass; id=$_.PNPDeviceID } })
$cls = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Group-Object Class | ForEach-Object { [pscustomobject]@{ cls=$_.Name; n=$_.Count; bad=@($_.Group | Where-Object { $_.Status -ne 'OK' }).Count } })
$ev = @(Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2; StartTime=(Get-Date).AddDays(-7)} -MaxEvents 60 -ErrorAction SilentlyContinue | ForEach-Object {
  [pscustomobject]@{ src=$_.ProviderName; id=$_.Id; level=$_.Level; t=$_.TimeCreated.ToString('o'); msg=(($_.Message -split "`n")[0]).Trim() } })
$os = Get-CimInstance Win32_OperatingSystem
$fast = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -ErrorAction SilentlyContinue).HiberbootEnabled
[pscustomobject]@{ errors=$err; classes=$cls; events=$ev; boot=$os.LastBootUpTime.ToString('o'); fastStartup=($fast -eq 1) }""", timeout=60) or {}
    errs = r.get("errors") or []
    if isinstance(errs, dict):
        errs = [errs]
    for e in errs:
        c = e.get("code")
        e["why"] = tr(CM_CODES[c]) if c in CM_CODES else tr("Error code {code}", code=c)
    evs = r.get("events") or []
    if isinstance(evs, dict):
        evs = [evs]
    grouped = {}
    for e in evs:
        k = (e.get("src"), e.get("id"))
        g = grouped.get(k)
        if g:
            g["count"] += 1
            continue
        e["count"] = 1
        e["help"] = tr(EVENT_HELP[k]) if k in EVENT_HELP else None
        grouped[k] = e
    cls = r.get("classes") or []
    if isinstance(cls, dict):
        cls = [cls]
    return {"errors": errs, "classes": cls, "events": list(grouped.values())[:12], "boot": r.get("boot"),
            "fastStartup": r.get("fastStartup"), "admin": _admin()}

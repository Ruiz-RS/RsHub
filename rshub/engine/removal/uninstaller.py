"""Running uninstallers.

NSIS and Inno copy themselves to TEMP, launch the copy and the original process exits immediately.
That is why the uninstaller runs inside a Windows Job Object and we wait until the WHOLE job
(with all its children) is empty, not just the first process.
"""
import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes

from ...i18n import tr
from . import index, registry, system
from . import programs as progmod
from .registry import RegRef

_k32 = ctypes.windll.kernel32
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_ntdll = ctypes.windll.ntdll
CREATE_SUSPENDED = 0x4


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


def _job_active(job):
    info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    ok = _k32.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None)
    return info.ActiveProcesses if ok else -1, info.TotalProcesses


def run_tracked(cmd, cancel=None, timeout=3600, on_tick=None):
    """Run cmd and wait until it and all its descendants have finished.
    Return dict(exit_code, processes, seconds, cancelled, timeout)."""
    cancel = cancel or threading.Event()
    job = _k32.CreateJobObjectW(None, None)
    t0 = time.time()
    proc = subprocess.Popen(cmd, shell=True, creationflags=CREATE_SUSPENDED)
    try:
        assigned = bool(job) and bool(_k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))))
        _ntdll.NtResumeProcess(wintypes.HANDLE(int(proc._handle)))
        total = 1
        while True:
            if cancel.is_set():
                return {"exit_code": None, "processes": total, "seconds": time.time() - t0, "cancelled": True,
                        "timeout": False}
            if time.time() - t0 > timeout:
                return {"exit_code": None, "processes": total, "seconds": time.time() - t0, "cancelled": False,
                        "timeout": True}
            if assigned:
                active, total = _job_active(job)
                if active == 0:
                    break
            elif proc.poll() is not None:
                break
            if on_tick:
                on_tick(time.time() - t0, total)
            time.sleep(0.4)
        return {"exit_code": proc.poll(), "processes": total, "seconds": round(time.time() - t0, 1),
                "cancelled": False, "timeout": False}
    finally:
        if job:
            _k32.CloseHandle(job)


def still_registered(p):
    if p.get("source") == "store":
        code, out = system.powershell(f"if (Get-AppxPackage -Name '{p.get('package_name', '')}') {{ 'yes' }}")
        return "yes" in out
    if p.get("msi_code") and progmod.msi_installed(p["msi_code"]):
        return True
    return bool(p.get("key")) and registry.key_exists(RegRef.from_str(p["key"]))


def prepare(p):
    """What we need to know before uninstalling."""
    locs = [p.get("location")] if p.get("location") and os.path.isdir(p.get("location")) else []
    procs = system.processes_in(locs) if locs else []
    cmd, quiet = progmod.uninstall_command(p, quiet=False)
    qcmd, can_q = progmod.uninstall_command(p, quiet=True)
    return {"pending_reboot": system.pending_reboot(), "admin": system.is_admin(),
            "processes": [{"pid": x["pid"], "name": x["name"], "path": x["path"]} for x in procs],
            "command": cmd, "quiet_command": qcmd if can_q else None, "kind": p.get("kind"),
            "kind_label": p.get("kind_label"), "has_uninstaller": bool(cmd) or p.get("source") == "store",
            "protected": p.get("protected"), "broken": p.get("broken")}


def close_processes(p):
    """Close the processes running from the program folder."""
    loc = p.get("location")
    if not loc or not os.path.isdir(loc):
        return []
    closed = []
    for pr in system.processes_in([loc]):
        if system.kill(pr["pid"]):
            closed.append(pr["name"])
    time.sleep(0.6 if closed else 0)
    return closed


def uninstall(p, quiet=False, cancel=None, on_tick=None):
    """Run the official uninstaller. Return a dict with the result."""
    if p.get("source") == "store":
        t0 = time.time()
        # by family (name + publisher), not by full name: the list may come from the cache and the app
        # may have been updated since (the full name includes the version)
        name, fam = p.get("package_name", "").replace("'", ""), p.get("family", "").replace("'", "")
        if name and fam:
            cmd = (f"$p = @(Get-AppxPackage -Name '{name}' | Where-Object {{ $_.PackageFamilyName -eq '{fam}' }}); "
                   f"if (-not $p) {{ throw 'The app is no longer installed' }}; $p | Remove-AppxPackage -ErrorAction Stop")
        else:
            cmd = f"Remove-AppxPackage -Package '{p['package']}' -ErrorAction Stop"
        code, out = system.powershell(cmd, timeout=600)
        index.invalidate()
        return {"ok": code == 0, "exit_code": code, "seconds": round(time.time() - t0, 1), "output": out[-400:],
                "still_registered": still_registered(p), "command": "Remove-AppxPackage"}
    cmd, is_quiet = progmod.uninstall_command(p, quiet)
    if not cmd:
        return {"ok": False, "error": tr("The program has no uninstaller"), "still_registered": True}
    r = run_tracked(cmd, cancel=cancel, on_tick=on_tick)
    # Steam and other launchers hand off to a process that was already running: wait for the entry to disappear
    if not r["cancelled"] and p.get("kind") in ("steam",):
        t0 = time.time()
        while still_registered(p) and time.time() - t0 < 300 and not (cancel and cancel.is_set()):
            if on_tick:
                on_tick(time.time() - t0, r["processes"])
            time.sleep(1)
    time.sleep(0.5)
    index.invalidate()
    reg = still_registered(p)
    return {"ok": not reg and not r["cancelled"], "exit_code": r["exit_code"], "seconds": r["seconds"],
            "processes": r["processes"], "cancelled": r["cancelled"], "timeout": r["timeout"],
            "still_registered": reg, "command": cmd, "quiet": is_quiet}

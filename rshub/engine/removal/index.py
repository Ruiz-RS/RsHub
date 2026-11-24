"""System index: built once and shared by every scan.

Reading Classes/CLSID, tasks and services takes seconds; looking things up in the index, microseconds.
It is invalidated whenever the remover changes something (delete, restore, uninstall).
"""
import os
import threading
import time
import winreg
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import paths, registry, shortcuts, system
from .registry import V32, V64

_lock = threading.RLock()
_data = {}
_built_at = {}
_building = {}   # name -> Lock: two threads asking for the same section do not build it twice
_gen = 0         # bumped on every invalidation: a result built before it is not stored
TTL = 600  # seconds


def invalidate(*names):
    """Without arguments, the whole index; with names, only those sections."""
    global _gen
    with _lock:
        _gen += 1
        for n in (names or list(_data)):
            _data.pop(n, None)
            _built_at.pop(n, None)


def _fresh(name):
    return name in _data and time.time() - _built_at[name] < TTL


def _get(name, builder):
    with _lock:
        if _fresh(name):
            return _data[name]
        building = _building.setdefault(name, threading.Lock())
    with building:
        with _lock:
            if _fresh(name):
                return _data[name]
            gen = _gen
        val = builder()
        with _lock:
            if gen == _gen:
                _data[name] = val
                _built_at[name] = time.time()
        return val


# ---------------------------------------------------------------- sections
def services():
    return _get("services", system.list_services)


def tasks():
    return _get("tasks", system.list_tasks)


def run_values():
    def b():
        out = []
        for hive, view in (("HKCU", 0), ("HKLM", V64), ("HKLM", V32)):
            for sub in ("Run", "RunOnce"):
                base = rf"Software\Microsoft\Windows\CurrentVersion\{sub}"
                for name, data, _ in registry.values(registry.HIVES[hive], base, view):
                    out.append((hive, base, view, name, str(data)))
        return out
    return _get("run", b)


def startup_approved():
    def b():
        out = {}
        for hive in ("HKCU", "HKLM"):
            for sa in ("Run", "Run32", "StartupFolder"):
                base = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\{sa}"
                for name, data, _ in registry.values(registry.HIVES[hive], base):
                    out.setdefault(name.lower(), []).append((hive, base, name, data))
        return out
    return _get("startup_approved", b)


def app_paths():
    def b():
        out = []
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for hive, view in (("HKLM", V64), ("HKLM", V32), ("HKCU", 0)):
            root = registry.HIVES[hive]
            for sub in registry.subkeys(root, base, view):
                path = f"{base}\\{sub}"
                out.append((hive, view, path, sub, str(registry.get_value(root, path, "", view) or ""),
                            str(registry.get_value(root, path, "Path", view) or "")))
        return out
    return _get("app_paths", b)


def uninstall_entries():
    def b():
        from .programs import SOURCES, UNINSTALL
        out = []
        for hive, view in SOURCES:
            root = registry.HIVES[hive]
            for kn in registry.subkeys(root, UNINSTALL, view):
                path = f"{UNINSTALL}\\{kn}"
                vals = {n: str(d) for n, d, _ in registry.values(root, path, view)}
                out.append((hive, view, kn, path, vals))
        return out
    return _get("uninstall", b)


def shared_dlls():
    def b():
        out = []
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs"
        for view in registry.hklm_views():
            for name, data, _ in registry.values(winreg.HKEY_LOCAL_MACHINE, base, view):
                out.append((view, base, name, data))
        return out
    return _get("shared_dlls", b)


def firewall_rules():
    def b():
        fw = r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"
        return [(n, str(d)) for n, d, _ in registry.values(winreg.HKEY_LOCAL_MACHINE, fw)]
    return _get("firewall", b)


def env_paths():
    def b():
        out = []
        for scope, hive, path in (("user", winreg.HKEY_CURRENT_USER, "Environment"),
                                  ("machine", winreg.HKEY_LOCAL_MACHINE,
                                   r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            val = registry.get_value(hive, path, "Path") or ""
            out += [(scope, e) for e in str(val).split(";") if e]
        return out
    return _get("env", b)


def classes():
    """ProgIDs with their command/icon, COM servers and OpenWithProgids."""
    def b():
        progids, clsids, openwith, apps = [], [], [], set()
        for hive, base in (("HKLM", r"SOFTWARE\Classes"), ("HKCU", r"Software\Classes")):
            root = registry.HIVES[hive]
            for sub in registry.subkeys(root, base):
                low = sub.lower()
                if low.startswith("."):
                    for name, _, _ in registry.values(root, rf"{base}\{sub}\OpenWithProgids"):
                        openwith.append((hive, base, sub, name))
                    continue
                if low in ("clsid", "appid", "typelib", "interface", "installer", "wow6432node"):
                    continue
                if low == "applications":
                    apps |= {(hive, a.lower()) for a in registry.subkeys(root, rf"{base}\Applications")}
                    continue
                cmd = registry.get_value(root, rf"{base}\{sub}\shell\open\command", "")
                icon = registry.get_value(root, rf"{base}\{sub}\DefaultIcon", "")
                if cmd or icon:
                    progids.append((hive, base, sub, str(cmd or ""), str(icon or "")))
            for clsid in registry.subkeys(root, base + r"\CLSID"):
                cp = rf"{base}\CLSID\{clsid}"
                for srv in ("InprocServer32", "LocalServer32"):
                    v = registry.get_value(root, f"{cp}\\{srv}", "")
                    if v:
                        clsids.append((hive, cp, str(v)))
                        break
        return {"progids": progids, "clsids": clsids, "openwith": openwith, "apps": apps}
    return _get("classes", b)


def shortcut_files():
    def b():
        k = paths.known_folders()
        out = []
        for r in (k.get("Programs"), k.get("CommonPrograms")):
            if r and os.path.isdir(r):
                for dp, dn, fn in os.walk(r):
                    out += [os.path.join(dp, f) for f in fn if f.lower().endswith((".lnk", ".url"))]
        roam = k.get("RoamingAppData", "")
        for r in (k.get("Desktop"), k.get("PublicDesktop"), k.get("Startup"), k.get("CommonStartup"),
                  os.path.join(roam, "Microsoft", "Internet Explorer", "Quick Launch"),
                  os.path.join(roam, "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar")):
            try:
                out += [os.path.join(r, f) for f in os.listdir(r) if f.lower().endswith((".lnk", ".url"))]
            except (OSError, TypeError):
                pass
        return [(f, shortcuts.read_shortcut(f)) for f in out]
    return _get("shortcuts", b)


def warm(include_classes=True, progress=None):
    """Build the whole index at once (tasks wait for an external process; the rest reads the registry)."""
    steps = [tasks, classes if include_classes else None, services, shortcut_files, uninstall_entries,
             run_values, startup_approved, app_paths, shared_dlls, firewall_rules, env_paths]
    steps = [s for s in steps if s]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, _ in enumerate(as_completed([ex.submit(s) for s in steps])):
            if progress:
                progress((i + 1) / len(steps), "")

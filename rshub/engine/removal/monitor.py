"""Watched install: a snapshot of the system before and after; the difference is the program's trace.

Revo uses a kernel driver; here two snapshots are compared. It sees everything that remains created when
the installation finishes (which is what has to be removed later).
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from ...i18n import tr
from . import paths, registry, safety, system
from .paths import is_within, norm_path
from .registry import V32, V64, RegRef

NOISE = ("\\temp\\", "\\cache", "\\crashdumps", "\\logs\\", "\\microsoft\\", "\\packages\\", "\\windows\\",
         "\\inetcache", "\\history", "\\d3dscache", "\\nvidia\\dxcache", "\\gpucache")


def _file_roots():
    k = paths.known_folders()
    cf = k.get("ProgramFilesCommon")
    roots = [k.get("ProgramFiles"), k.get("ProgramFilesX86"), cf, k.get("ProgramFilesCommonX86"),
             k.get("ProgramData"), k.get("LocalAppData"), k.get("RoamingAppData"), k.get("LocalAppDataLow"),
             os.path.join(k.get("LocalAppData", ""), "Programs"), k.get("Programs"), k.get("CommonPrograms"),
             k.get("Desktop"), k.get("PublicDesktop"), k.get("Startup"), k.get("CommonStartup"), k.get("Documents")]
    if cf:
        roots += [os.path.join(cf, "VST3"), os.path.join(cf, "CLAP"), os.path.join(cf, "VST2")]
    return [r for r in dict.fromkeys(roots) if r and os.path.isdir(r)]


def _snap_root(r, depth):
    out = []
    stack = [(r, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    out.append(e.path.lower())
                    if lvl + 1 < depth:
                        try:
                            attrs = e.stat(follow_symlinks=False).st_file_attributes
                        except OSError:
                            continue
                        # a real folder (not a link or junction); the attribute comes from the listing
                        if attrs & 0x10 and not attrs & 0x400:
                            stack.append((e.path, lvl + 1))
        except OSError:
            pass
    return out


def _snap_files(depth=2):
    out = set()
    with ThreadPoolExecutor(max_workers=4) as ex:
        for part in ex.map(lambda r: _snap_root(r, depth), _file_roots()):
            out.update(part)
    return out


_REG_ROOTS = [("HKCU", "Software", 0, 2), ("HKLM", "SOFTWARE", V64, 2), ("HKLM", "SOFTWARE", V32, 2),
              ("HKCU", r"Software\Classes", 0, 1), ("HKLM", r"SOFTWARE\Classes", 0, 1),
              ("HKLM", r"SOFTWARE\Classes\CLSID", 0, 1),
              ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", V64, 1),
              ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", V32, 1),
              ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, 1),
              ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths", V64, 1),
              ("HKLM", r"SYSTEM\CurrentControlSet\Services", 0, 1)]
_VAL_ROOTS = [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", 0),
              ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", V64),
              ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", V32),
              ("HKLM", r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules", 0)]


def _snap_reg():
    keys, vals = set(), set()
    for hive, base, view, depth in _REG_ROOTS:
        stack = [(base, 0)]
        while stack:
            path, lvl = stack.pop()
            for s in registry.subkeys(registry.HIVES[hive], path, view):
                sp = f"{path}\\{s}"
                keys.add(RegRef(hive, sp, view).to_str())
                if lvl + 1 < depth and s.lower() not in ("microsoft", "classes", "wow6432node", "policies"):
                    stack.append((sp, lvl + 1))
    for hive, base, view in _VAL_ROOTS:
        for n, _, _ in registry.values(registry.HIVES[hive], base, view):
            vals.add(RegRef(hive, base, view, n).to_str())
    return keys, vals


def snapshot():
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_files = ex.submit(_snap_files)
        f_tasks = ex.submit(system.list_tasks)
        keys, vals = _snap_reg()
        tasks = {t["path"] for t in f_tasks.result()}
        files = f_files.result()
    return {"files": sorted(files), "keys": sorted(keys), "values": sorted(vals), "tasks": sorted(tasks),
            "time": time.time()}


def _mdir():
    return paths.data_path("monitor")


def start(name):
    mid = time.strftime("%Y%m%d-%H%M%S")
    snap = snapshot()
    d = os.path.join(_mdir(), mid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "before.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f)
    meta = {"id": mid, "name": name or tr("Watched install"), "started": time.time(), "status": "running"}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return meta


def _topmost(paths_set):
    """Drop the paths that are inside another path of the set."""
    out = []
    for p in sorted(paths_set, key=len):
        if not any(p.startswith(q + "\\") for q in out):
            out.append(p)
    return out


def stop(mid):
    d = os.path.join(_mdir(), mid)
    with open(os.path.join(d, "before.json"), encoding="utf-8") as f:
        before = json.load(f)
    after = snapshot()
    new_files = _topmost(set(after["files"]) - set(before["files"]))
    new_files = [f for f in new_files if not any(n in f + "\\" for n in NOISE) or
                 is_within(f, paths.kf("Programs") or "") or is_within(f, paths.kf("CommonPrograms") or "")]
    bk = set(before["keys"])
    new_keys = [k for k in after["keys"] if k not in bk]
    new_keys = _topmost_keys(new_keys)
    new_vals = [v for v in after["values"] if v not in set(before["values"])]
    new_tasks = [t for t in after["tasks"] if t not in set(before["tasks"])]
    items = []
    for f in new_files:
        real = _real_case(f)
        items.append({"kind": "folder" if os.path.isdir(real) else "file", "target": real, "display": real})
    for k in new_keys:
        ref = RegRef.from_str(k)
        if ref.path.lower().startswith(r"system\currentcontrolset\services"):
            items.append({"kind": "service", "target": ref.path.rsplit("\\", 1)[-1],
                          "display": f"Service: {ref.path.rsplit(chr(92), 1)[-1]}"})
        else:
            items.append({"kind": "regkey", "target": k, "display": ref.display()})
    for v in new_vals:
        ref = RegRef.from_str(v)
        if "firewallrules" in ref.path.lower():
            data = registry.get_value(registry.HIVES[ref.hive], ref.path, ref.value) or ""
            items.append({"kind": "fwrule", "target": ref.value, "display": f"Firewall: {ref.value}", "fw_data": data})
        else:
            items.append({"kind": "regvalue", "target": v, "display": ref.display()})
    for t in new_tasks:
        items.append({"kind": "task", "target": t, "display": f"Task: {t}"})
    uninstall_keys = [k for k in new_keys if "\\currentversion\\uninstall\\" in k.lower()]
    meta = _read_meta(mid)
    meta.update(status="done", stopped=time.time(), count=len(items), uninstall_keys=uninstall_keys)
    with open(os.path.join(d, "trace.json"), "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    try:
        os.remove(os.path.join(d, "before.json"))
    except OSError:
        pass
    return meta


def _topmost_keys(keys):
    out = []
    parsed = sorted(keys, key=lambda k: len(RegRef.from_str(k).path))
    seen = []
    for k in parsed:
        r = RegRef.from_str(k)
        pfx = (r.hive, r.view, r.path.lower())
        if any(pfx[0] == s[0] and pfx[1] == s[1] and pfx[2].startswith(s[2] + "\\") for s in seen):
            continue
        seen.append(pfx)
        out.append(k)
    return out


def _real_case(p):
    """Return the path with its real capitalization."""
    drive, rest = os.path.splitdrive(p)
    cur = drive.upper() + "\\"
    for part in [x for x in rest.split("\\") if x]:
        try:
            match = next((e for e in os.listdir(cur) if e.lower() == part.lower()), part)
        except OSError:
            match = part
        cur = os.path.join(cur, match)
    return cur


def _read_meta(mid):
    with open(os.path.join(_mdir(), mid, "meta.json"), encoding="utf-8") as f:
        return json.load(f)


def traces():
    out = []
    d = _mdir()
    for mid in sorted(os.listdir(d), reverse=True) if os.path.isdir(d) else []:
        try:
            out.append(_read_meta(mid))
        except (OSError, ValueError):
            pass
    return out


def rename(mid, name):
    meta = _read_meta(mid)
    meta["name"] = name
    with open(os.path.join(_mdir(), mid, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def delete(mid):
    d = os.path.join(_mdir(), mid)
    if is_within(d, _mdir()) and norm_path(d) != norm_path(_mdir()):
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def trace_items(mid, programs=()):
    """Trace items that still exist, with the same safety rules as the scanner."""
    with open(os.path.join(_mdir(), mid, "trace.json"), encoding="utf-8") as f:
        items = json.load(f)["items"]
    ctx = safety.Context(target=None, programs=programs)
    out = []
    for n, it in enumerate(items):
        k = it["kind"]
        exists, blocked, personal, size = True, None, False, 0
        if k in ("file", "folder"):
            exists = os.path.lexists(it["target"])
            if exists:
                blocked, personal = ctx.check_path(it["target"])
                size = paths.path_size(it["target"]) if not blocked else 0
        elif k in ("regkey", "regvalue"):
            ref = RegRef.from_str(it["target"])
            exists = registry.key_exists(ref) if k == "regkey" else registry.value_exists(ref)
            blocked = safety.reg_rule(ref)
        elif k == "service":
            exists = registry.key_exists(RegRef("HKLM", rf"SYSTEM\CurrentControlSet\Services\{it['target']}"))
            blocked = safety.service_rule(it["target"])
        if not exists:
            continue
        # trace.json keeps English labels; services, rules and tasks are shown in the current language
        disp = {"service": lambda: tr("Service: {name}", name=it["target"]),
                "fwrule": lambda: tr("Firewall: {name}", name=it["target"]),
                "task": lambda: tr("Task: {path}", path=it["target"])}.get(k)
        if disp:
            it = {**it, "display": disp()}
        out.append({**it, "id": f"t{n}", "score": 100, "reasons": [tr("Created during the watched install")],
                    "blocked": blocked, "personal": personal, "size": size, "level": "safe",
                    "selected": not blocked and not personal,
                    "group": "Blocked for safety" if blocked else ("Personal data" if personal else
                                                                        _group(k))})
    return out


def _group(kind):
    return {"folder": "Files and folders", "file": "Files and folders", "regkey": "Registry",
            "regvalue": "Registry", "service": "Services and tasks", "task": "Services and tasks",
            "fwrule": "Services and tasks"}.get(kind, "Other")

"""Super Optimizer engine: cleanup, startup, services, registry, repair and performance. For real.

Rules: anything that changes the system saves a copy first so it can be undone (service states, .reg, previous
settings); anything that needs administrator rights checks for them and returns {"needAdmin": True} instead of
failing halfway; services outside a closed list and registry keys that are not really broken are never touched.
"""
import codecs
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import winreg
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from ..i18n import tr
from .ps import ps, ps_json, gather, NO_WINDOW
from .removal import cleaner, registry, startup
from .removal.registry import RegRef, V32, V64
from .removal import system as rsys

EXPORTS = {"perf_read", "perf_set", "startup_set", "services_list", "services_apply", "services_undo",
           "registry_undo", "open_location", "open_startup_page", "boot_info", "bg_set", "power_plan_set",
           "startup_icons", "registry_backups_exist"}
JOBS = {"clean_scan", "clean", "startup_list", "registry_scan", "registry_fix", "repair", "restore_point",
        "services_undo_job", "registry_undo_job"}

# Machine-wide copies (services, HKLM registry keys): they are only written and read by elevated runs, so
# ProgramData's default permissions (read-only for ordinary users, nobody can tamper with them) are what we want.
DATA = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "RsHub", "optimizer")
# Per-user copies (HKCU registry keys, the Debloat and Privacy records). In the user's own profile: an elevated run
# can never leave them read-only for the same user's later non-elevated runs, and other users can't read them.
USER_DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
                         "RsHub", "optimizer")


def new_bid():
    """Backup id: second-resolution time plus 6 random hex digits, so two backups in the same second don't collide."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def unique_bid(*folders):
    """A backup id that doesn't exist yet in any of the folders."""
    for _ in range(200):
        bid = new_bid()
        if not any(os.path.exists(os.path.join(f, bid)) or os.path.exists(os.path.join(f, bid + ".json")) for f in folders):
            return bid
    raise OSError("no free backup id")


def write_json_atomic(path, data):
    """Writes a JSON file through a temporary file. Raises OSError when it can't be written."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def user_needs_admin(hive, path):
    """HKLM, and HKCU\\Software\\Policies (an ordinary user only has ReadKey there), need administrator rights."""
    h = str(hive).upper()
    return h == "HKLM" or (h == "HKCU" and (path or "").replace("/", "\\").lstrip("\\").lower().startswith("software\\policies"))


def _admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _need_admin():
    return {"needAdmin": True}


def _short(p, n=64):
    p = p.replace(os.path.expanduser("~"), "%USERPROFILE%")
    return p if len(p) <= n else p[:18] + "…" + p[-(n - 19):]


# =============================================================== cleanup
def _clean_opts(opts):
    """Options from the interface: days to keep temp files (1/7/30) and folders to always/now skip."""
    opts = opts or {}
    days = opts.get("days", cleaner.TEMP_GRACE_DAYS)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = cleaner.TEMP_GRACE_DAYS
    if days not in cleaner.GRACE_CHOICES:
        days = cleaner.TEMP_GRACE_DAYS
    exclude = [str(x) for x in (opts.get("exclude") or []) if str(x).strip()]
    return days, exclude


def clean_scan(job, opts=None):
    """Measures every cleaner category for real (without deleting anything). For temp folders it also returns
    one row per first-level folder saying whether it will be emptied or why it is kept."""
    days, exclude = _clean_opts(opts)
    cats = cleaner.categories()
    out = []
    job.progress(0, tr("Checking which folders running programs are using…"), force=True)
    # live progress: the query "which program uses what" (weight W_IU), the first-level folders of the temp
    # categories, and one unit per finished category, so the bar always moves and the text says what is happening
    W_IU, n_cats = 3.0, max(1, len(cats))
    total_w = W_IU + n_cats + 6.0 * sum(1 for c in cats.values() if c.get("temp"))   # temp categories also read their folders
    st = {"iu": 0.0, "iu_txt": "", "walk": {}, "walk_txt": "", "pct": 0.0}
    lock = threading.RLock()

    def report(force=False):
        with lock:
            frac = (W_IU * st["iu"] + len(out) + 6.0 * sum(st["walk"].values())) / total_w
            st["pct"] = 1.0 if len(out) >= n_cats else max(st["pct"], min(.99, frac))
            txt = st["walk_txt"] if any(v < 1 for v in st["walk"].values()) else st["iu_txt"] or st["walk_txt"]
            job.progress(st["pct"], txt or tr("Checking which folders running programs are using…"), force=force,
                         done=len(out), total=n_cats)

    def on_inuse(i, n):
        with lock:
            st["iu"] = i / max(1, n)
            st["iu_txt"] = tr("Checking which programs have files open… {i} of {n}", i=f"{i:,}", n=f"{n:,}")
            report()

    def on_tick(cid, i, n):
        with lock:
            st["walk"][cid] = .9 * i / max(1, n)       # the last tenth is the wait for the programs query
            st["walk_txt"] = tr("Reading {name}… {i} of {n} folders", name=tr(cats[cid]["label"]).lower(), i=f"{i:,}", n=f"{n:,}")
            report()

    def on_item(it):
        c = cats[it["id"]]
        dirs, files = ([], []) if c.get("recycle") else cleaner._expand(c)
        pths = [_short(d) for d in dirs[:4]] + [_short(f) for f in files[:2]]
        if c.get("recycle"):
            pths = [tr("All drives")]
        row = {"id": it["id"], "g": it["group"], "label": it["label"], "size": it["size"], "files": it["count"],
               "def": bool(c.get("default")), "admin": bool(c.get("admin")), "needs_admin": it["needs_admin"],
               "busy": it["process"] if it["busy"] else "", "desc": it["desc"] or tr(_DESC.get(it["id"], "")),
               "paths": pths or [tr("(not present on this PC)")], "temp": bool(c.get("temp"))}
        if "rows" in it:
            row["rows"] = [{**r, "short": _short(r["path"], 70)} for r in it["rows"]]
            row["keptSize"], row["keptCount"] = it.get("keptSize", 0), it.get("keptCount", 0)
        with lock:
            out.append(row)
            if it["id"] in st["walk"]:
                st["walk"][it["id"]] = 1.0
            st["walk_txt"] = ""
            if st["iu"] >= 1 or not st["iu_txt"]:
                st["iu_txt"] = tr("Measured {done} of {total} categories", done=len(out), total=n_cats)
            report(force=len(out) == n_cats)

    cleaner.scan(on_item=on_item, days=days, exclude=exclude, on_inuse=on_inuse, on_tick=on_tick)
    order = list(cats)
    out.sort(key=lambda x: order.index(x["id"]))
    return out


_DESC = {
    "win_temp": "Leftovers from installations and system tasks.",
    "dumps": "Memory copies of programs that crashed. Only useful for debugging.",
    "do_cache": "Pieces of updates your PC shares with other computers.",
    "recycle": "Things you deleted. Once emptied, they can't be recovered.",
    "chrome": "Page, code and GPU cache. Doesn't touch passwords, history or tabs.",
    "edge": "Page, code and GPU cache. Doesn't touch passwords, history or tabs.",
    "brave": "Page, code and GPU cache. Doesn't touch passwords, history or tabs.",
    "opera": "Page cache.", "firefox": "Page and startup cache.",
    "discord": "Images and videos you've already seen.",
    "spotify": "Cached songs that aren't downloaded for offline listening.",
    "steam_web": "Store and community cache.",
}


def clean(job, ids, opts=None):
    """Cleans the chosen categories with real progress (files deleted of N, bytes freed, current folder, speed,
    time left) and a real Cancel. Skips categories that need administrator rights or whose app is open (deleting
    a running browser's cache can corrupt it), and temp folders that are recent, in use or excluded."""
    days, exclude = _clean_opts(opts)
    cats = cleaner.categories()
    admin = _admin()
    procs = {p["name"].lower() for p in rsys.list_processes()}
    total = {"freed": 0, "deleted": 0, "skipped": 0, "per": {}, "skippedCats": [], "busyCats": [], "cancelled": False}
    run = []
    for cid in ids:
        c = cats.get(cid)
        if not c:
            continue
        if c.get("admin") and not admin:
            total["skippedCats"].append(cid)
        elif cleaner.running(c.get("process"), procs):
            total["busyCats"].append(cid)
        else:
            run.append(cid)
    job.progress(0, tr("Checking which folders running programs are using…"), force=True)

    def on_progress(d):
        tot = max(1, d.get("total") or 0)
        pct = (d["deleted"] + d["skipped"]) / tot if d.get("total") else 0
        cur = d.get("current") or ""
        data = dict(d, current=_short(cur, 60) if os.path.isabs(cur) else cur)
        job.progress(min(.99, pct), tr("Deleted {n} of {total} files", n=f"{d['deleted']:,}", total=f"{d.get('total', 0):,}"), **data)

    r = cleaner.clean(run, days=days, exclude=exclude, cancel=lambda: job.cancelled, on_progress=on_progress)
    for k in ("freed", "deleted", "skipped", "cancelled"):
        total[k] = r[k]
    total["per"] = r["per"]
    for cid, freed in r["per"].items():
        job.progress(None, tr(cats[cid]["label"]), id=cid, catFreed=freed, force=True)
    return total


# =============================================================== startup
_PF_TASK = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData"
_IMPACT_CACHE = {}


def _packaged_startup():
    """Store apps with a startup task (what Task Manager lists besides Run and the Startup folder)."""
    with ThreadPoolExecutor(max_workers=2) as ex:  # both queries at once
        f_pk = ex.submit(ps_json, "Get-AppxPackage -PackageTypeFilter Main | Select-Object Name, PackageFamilyName, PackageFullName, "
                         "InstallLocation, @{n='Pub';e={$_.PublisherDisplayName}}", timeout=60, as_list=True)
        f_sa = ex.submit(_start_apps)
        pk, start_apps = f_pk.result(), f_sa.result()
    names = {}
    for a in start_apps:
        app_id = a.get("AppID") or ""
        if "!" in app_id:
            names.setdefault(app_id.split("!")[0], a.get("Name"))
    out = []

    def one(p):
        loc = p.get("InstallLocation") or ""
        mf = os.path.join(loc, "AppxManifest.xml")
        if not loc or not os.path.isfile(mf):
            return
        try:
            txt = open(mf, "rb").read().decode("utf-8", "ignore")
        except OSError:
            return
        if "windows.startupTask" not in txt:
            return
        try:
            root = ET.fromstring(txt.encode("utf-8"))
        except ET.ParseError:
            return
        pubname = ""
        for el in root.iter():
            if el.tag.endswith("PublisherDisplayName") and el.text:
                pubname = el.text.strip()
        for ext in root.iter():
            if not ext.tag.endswith("Extension") or ext.get("Category") != "windows.startupTask":
                continue
            for st in ext:
                if not st.tag.endswith("StartupTask"):
                    continue
                tid = st.get("TaskId")
                default_on = (st.get("Enabled") or "false").lower() == "true"
                pfn = p.get("PackageFamilyName")
                state = registry.get_value(winreg.HKEY_CURRENT_USER, f"{_PF_TASK}\\{pfn}\\{tid}", "State")
                # 0 disabled, 1 disabled by the user, 2 enabled, 3 disabled by policy, 4 enabled by policy
                on = state in (2, 4) if state is not None else default_on
                dn = names.get(pfn) or st.get("DisplayName") or p.get("Name")
                if dn and (dn.startswith("ms-resource:") or dn == p.get("Name")):
                    dn = (_resource(p, dn) if dn.startswith("ms-resource:") else None) or _manifest_name(p, root) or p.get("Name").split(".")[-1]
                out.append({"id": f"pkg|{pfn}|{tid}", "name": dn, "pub": p.get("Pub") or pubname, "on": on,
                            "origin": "store", "path": tr("App startup task ({id})", id=tid), "target": loc,
                            "locked": state in (3, 4), "isrc": _store_icon_src(loc, root)})
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, pk))
    return out


_SA = {"t": 0, "d": [], "lock": threading.Lock()}


def _start_apps():
    """Get-StartApps (Start menu apps), shared by Startup and Performance for a minute: it's a slow query."""
    with _SA["lock"]:
        if time.time() - _SA["t"] > 60:
            _SA["d"] = ps_json("Get-StartApps | Select-Object Name, AppID", timeout=40, as_list=True)
            _SA["t"] = time.time()
        return _SA["d"]


def _store_icon_src(loc, root):
    """The small logo of a Store app (Square44x44Logo, the one Start and Task Manager show)."""
    rel = ""
    for el in root.iter():
        if el.tag.endswith("VisualElements") and el.get("Square44x44Logo"):
            rel = el.get("Square44x44Logo")
            break
    if not rel:
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == "Logo" and el.text:
                rel = el.text.strip()
                break
    if not rel:
        return ""
    from .removal.programs import _store_logo
    png = _store_logo(loc, rel)
    return "png:" + png if png else ""


# script hosts: their own icon says nothing about what runs, so the UI shows a script icon instead
_SCRIPT_HOSTS = ("wscript.exe", "cscript.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "mshta.exe", "rundll32.exe",
                 "conhost.exe")


def icon_src(target, origin):
    """What to take a startup entry's icon from: "png:<file>" (Store), "exe:<file>", "script" or ""."""
    t = (target or "").strip().strip('"')
    if not t:
        return ""
    if os.path.basename(t).lower() in _SCRIPT_HOSTS:
        return "script"
    if origin == "store":
        return ""
    t = os.path.expandvars(t)
    if t.lower().endswith((".exe", ".dll", ".ico", ".lnk", ".bat", ".cmd", ".vbs", ".ps1", ".js")):
        if t.lower().endswith((".bat", ".cmd", ".vbs", ".ps1", ".js")):
            return "script"
        return "exe:" + t
    return ""


def _icon_of(src, extract=True):
    from .removal import icons
    try:
        if src.startswith("png:"):
            return icons.get_icon({"source": "store", "icon": src[4:]}, extract=extract)
        if src.startswith("exe:"):
            return icons.get_icon({"source": "file", "icon": src[4:]}, extract=extract)
    except Exception:
        return None
    return None


def startup_icons(srcs):
    """Real icons for the startup list (PNG data URIs), extracted in parallel; cached on disk afterwards."""
    srcs = [s for s in (srcs or []) if isinstance(s, str) and s[:4] in ("png:", "exe:")][:80]
    with ThreadPoolExecutor(max_workers=6) as ex:
        return {s: ic for s, ic in zip(srcs, ex.map(_icon_of, srcs)) if ic}


def _resource(p, ref):
    """Resolves a Store app 'ms-resource:' to its text (like File Explorer does)."""
    name = p.get("Name") or ""
    res = ref[len("ms-resource:"):]
    if not res.startswith("//"):
        res = f"//{name}/{res if '/' in res else 'Resources/' + res}"
    buf = ctypes.create_unicode_buffer(512)
    src = f"@{{{p.get('PackageFullName')}? ms-resource:{res}}}".replace("? ", "?")
    try:
        if ctypes.windll.shlwapi.SHLoadIndirectString(src, buf, 512, None) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return None


def _manifest_name(p, root):
    for el in root.iter():
        if el.tag.endswith("VisualElements") and el.get("DisplayName"):
            dn = el.get("DisplayName")
            return _resource(p, dn) if dn.startswith("ms-resource:") else dn
    return None


def _impacts():
    """Impact measured by Windows (the same one Task Manager uses). Only readable as administrator."""
    if _IMPACT_CACHE.get("t", 0) > time.time() - 600:
        return _IMPACT_CACHE["d"]
    data = {}
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "wdi", "LogFiles", "StartupInfo")
    try:
        files = sorted(glob.glob(os.path.join(folder, "*.xml")), key=os.path.getmtime)
    except OSError:
        files = []
    for f in files[-3:]:
        try:
            root = ET.parse(f).getroot()
        except (OSError, ET.ParseError):
            continue
        for p in root.iter():
            if not p.tag.endswith("Process"):
                continue
            name = (p.get("Name") or "").lower()
            vals = {c.tag.split("}")[-1]: c.text for c in p}
            try:
                cpu = float(vals.get("CpuUsage") or 0) / 1000  # ms
                disk = float(vals.get("DiskUsage") or 0)  # bytes
            except ValueError:
                continue
            lvl = "high" if cpu > 1000 or disk > 3e6 else "medium" if cpu > 300 or disk > 3e5 else "low"
            data[os.path.basename(name)] = lvl
    _IMPACT_CACHE.update(t=time.time(), d=data)
    return data


_REC = ("steam", "discord", "spotify", "epicgames", "onedrive", "teams", "skype", "adobe", "creative cloud",
        "battle.net", "origin", "ea desktop", "ubisoft", "dropbox", "googledrive", "zoom", "riot", "roblox")
_REVIEW = ("wscript.exe", "cscript.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "mshta.exe", "rundll32.exe")


def startup_list(job):
    """The same as Task Manager > Startup apps: registry (Run), Startup folder and Store apps."""
    job.progress(.1, tr("Registry and Startup folder"))

    def packaged():
        try:
            return _packaged_startup()
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=3) as ex:  # the three sources at once
        f_base, f_pk, f_imp = ex.submit(startup.list_entries), ex.submit(packaged), ex.submit(_impacts)
        base = [e for e in f_base.result() if e["type"] in ("run", "folder")]
        job.progress(.4, tr("Store apps"))
        pk = f_pk.result()
        job.progress(.85, tr("Startup impact"))
        imp = f_imp.result()
    measured = bool(imp)
    groups = {}
    for e in base:
        exe = e.get("exe") or ""
        key = os.path.basename(exe).lower() or e["name"].lower()
        groups.setdefault(key, []).append(e)
    items = []
    for key, es in groups.items():
        e = es[0]
        on = any(x["enabled"] for x in es)
        exe_name = os.path.basename(e.get("exe") or "") or e["name"]
        level = ("none" if not on else imp.get(exe_name.lower(), "unmeasured" if not measured else "low"))
        hk = "HKLM" if any(x["id"].startswith("run|HKLM") or x["id"].startswith("folder|HKLM") for x in es) else "HKCU"
        it = {"id": "grp|" + key, "ids": [x["id"] for x in es], "name": exe_name, "pub": e.get("company") or "",
              "on": on, "impact": level, "origin": "folder" if e["type"] == "folder" else "registry",
              "path": (f"{e['where']}: {e['name']}" if len(es) == 1 else f"{e['where']}"),
              "target": e.get("exe") or "", "hklm": hk == "HKLM", "missing": any(x.get("missing") for x in es),
              "children": [x["command"] for x in es] if len(es) > 1 or e["type"] == "folder" else [],
              "count": len(es) if len(es) > 1 else 0}
        it["isrc"] = icon_src(e.get("exe") or "", it["origin"])
        if exe_name.lower() in _REVIEW:
            it["review"] = True
            it["note"] = tr("Runs a script when you sign in. Check what it is before touching it: it may be yours.")
        if any(r in (exe_name + " " + it["pub"]).lower() for r in _REC) and on:
            it["rec"] = True
        if exe_name.lower() == "securityhealthsystray.exe":
            it["note"] = tr("The Windows Security icon in the system tray. Best left on.")
        items.append(it)
    for p in pk:
        p["ids"] = [p["id"]]
        p["impact"] = "none" if not p["on"] else imp.get((p["name"] or "").lower() + ".exe", "unmeasured" if not measured else "low")
        if p["on"] and any(r in (p["name"] + " " + p["pub"]).lower() for r in _REC):
            p["rec"] = True
        items.append(p)
    for it in items:  # icons already on disk come right away; the UI asks for the rest (startup_icons)
        src = it.get("isrc") or ""
        it["icon"] = _icon_of(src, extract=False) if src[:4] in ("png:", "exe:") else None
    job.progress(1, tr("Done"))
    return {"items": items, "measured": measured, "admin": _admin()}


def startup_set(ids, enabled):
    """Enables or disables like Task Manager does (reversible). ids: those of the entry (it can be a group)."""
    done = []
    for i in ids:
        if i.startswith("pkg|"):
            _, pfn, tid = i.split("|", 2)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"{_PF_TASK}\\{pfn}\\{tid}", 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "State", 0, winreg.REG_DWORD, 2 if enabled else 1)
        else:
            if (i.startswith("run|HKLM") or i.startswith("folder|HKLM")) and not _admin():
                return _need_admin()
            startup.set_enabled(i, bool(enabled))
        done.append(i)
    return {"ok": True, "changed": done}


def boot_info():
    """BIOS time of the last boot (the same Task Manager shows) and the latest boots measured by Windows
    (the latter needs administrator rights)."""
    bios = registry.get_value(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Power", "FwPOSTTime")
    boots = []
    if _admin():
        ev = ps_json("Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Diagnostics-Performance/Operational'; Id=100} "
                     "-MaxEvents 7 -ErrorAction SilentlyContinue | ForEach-Object { $x=[xml]$_.ToXml(); "
                     "[pscustomobject]@{ t=$_.TimeCreated.ToString('o'); ms=[int](($x.Event.EventData.Data | Where-Object Name -eq 'MainPathBootTime').'#text') } }",
                     timeout=30, as_list=True)
        boots = [{"t": e.get("t"), "s": round((e.get("ms") or 0) / 1000, 1)} for e in reversed(ev) if e.get("ms")]
    return {"bios": round(bios / 1000, 1) if isinstance(bios, int) else None, "boots": boots, "admin": _admin()}


def open_location(path):
    p = os.path.expandvars(path or "")
    if os.path.isfile(p):
        subprocess.Popen(["explorer.exe", "/select,", p], creationflags=NO_WINDOW)
        return True
    if os.path.isdir(p):
        os.startfile(p)
        return True
    return False


def open_startup_page():
    subprocess.Popen(["taskmgr.exe", "/7", "/startup"], creationflags=NO_WINDOW)
    return True


# =============================================================== services
# Closed list: only these can be changed from RsHub. Nothing critical to the system.
SERVICES = [
    {"id": "DiagTrack", "svc": ["DiagTrack"], "name": "Windows telemetry", "desc": "Sends usage and diagnostic data to Microsoft.", "bal": "disabled", "max": "disabled"},
    {"id": "dmwappushservice", "svc": ["dmwappushservice"], "name": "Telemetry messages (WAP Push)", "desc": "Helps telemetry receive messages.", "bal": "disabled", "max": "disabled"},
    {"id": "Fax", "svc": ["Fax"], "name": "Fax", "desc": "Send and receive faxes.", "bal": "disabled", "max": "disabled"},
    {"id": "RetailDemo", "svc": ["RetailDemo"], "name": "Retail demo mode", "desc": "Only used by display PCs in stores.", "bal": "disabled", "max": "disabled"},
    {"id": "MapsBroker", "svc": ["MapsBroker"], "name": "Downloaded maps", "desc": "Updates offline maps for the Maps app.", "bal": "manual", "max": "disabled"},
    {"id": "RemoteRegistry", "svc": ["RemoteRegistry"], "name": "Remote registry", "desc": "Lets another computer change this PC's registry.", "bal": "disabled", "max": "disabled"},
    {"id": "WerSvc", "svc": ["WerSvc"], "name": "Error reporting", "desc": "Sends reports to Microsoft when something fails.", "bal": None, "max": "disabled"},
    {"id": "Spooler", "svc": ["Spooler"], "name": "Print spooler", "desc": "Needed to print.", "bal": "manual", "max": "disabled", "printers": True},
    {"id": "SysMain", "svc": ["SysMain"], "name": "SysMain (app preloading)", "desc": "Loads the apps you use most into memory. On a fast SSD it's barely noticeable.", "bal": None, "max": "manual"},
    {"id": "WSearch", "svc": ["WSearch"], "name": "Search indexing", "desc": "Builds the index that makes file search instant.", "bal": None, "max": "manual", "warn": "Searching for files from Start will be slower."},
    {"id": "Xbox", "svc": ["XblAuthManager", "XblGameSave", "XboxNetApiSvc", "XboxGipSvc"], "name": "Xbox services", "desc": "Xbox Live sign-in, saved games and networking.", "bal": None, "max": "manual", "warn": "If you play with Game Pass, use the Xbox app, Game Bar or an Xbox controller, leave them on (Xbox Accessory Management is what controllers and Game Bar rely on)."},
    {"id": "lfsvc", "svc": ["lfsvc"], "name": "Geolocation", "desc": "Gives your location to apps that ask for it.", "bal": None, "max": "disabled", "warn": "Apps that use your location will no longer know it."},
    {"id": "PhoneSvc", "svc": ["PhoneSvc"], "name": "Phone service", "desc": "Calls from the PC through your phone.", "bal": None, "max": "manual"},
    {"id": "WbioSrvc", "svc": ["WbioSrvc"], "name": "Biometrics", "desc": "Windows Hello fingerprint and face recognition.", "bal": None, "max": None, "warn": "If you sign in with your fingerprint or face, don't touch it."},
]
_START = {"Auto": "auto", "Automatic": "auto", "Manual": "manual", "Disabled": "disabled", "Boot": "auto", "System": "auto"}
_START_PS = {"auto": "Automatic", "manual": "Manual", "disabled": "Disabled"}


def _printer_names():
    """Installed printers (local and network connections) straight from the print spooler API, without PowerShell.
    None if the API itself failed in an unexpected way (the caller then asks Windows through PowerShell)."""
    from ctypes import wintypes
    spool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    enum = spool.EnumPrintersW
    enum.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]

    class INFO4(ctypes.Structure):
        _fields_ = [("name", wintypes.LPWSTR), ("server", wintypes.LPWSTR), ("attributes", wintypes.DWORD)]
    need, count = wintypes.DWORD(), wintypes.DWORD()
    flags = 0x2 | 0x4  # PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    enum(flags, None, 4, None, 0, ctypes.byref(need), ctypes.byref(count))
    err = ctypes.get_last_error()
    if not need.value:
        # 0 printers, or the spooler is not running (RPC server unavailable): Windows has none to list either way
        return [] if err in (0, 1722, 1726, 1753) else None
    buf = ctypes.create_string_buffer(need.value)
    if not enum(flags, None, 4, buf, need.value, ctypes.byref(need), ctypes.byref(count)):
        return None
    arr = ctypes.cast(buf, ctypes.POINTER(INFO4))
    return [arr[i].name for i in range(count.value) if arr[i].name]


def _printers():
    try:
        names = _printer_names()
    except Exception:
        names = None
    if names is None:
        pr = ps_json("Get-CimInstance Win32_Printer | Select-Object Name", timeout=20, as_list=True)
        names = [p["Name"] for p in pr if p.get("Name")]
    virt = ("pdf", "xps", "onenote", "fax", "anydesk", "send to", "evernote", "snagit", "cutepdf", "writer")
    return [n for n in names if not any(v in n.lower() for v in virt)]


def _service_rows(names):
    """{name: {"Name", "State", "StartMode"}} for the services that exist, read through the service manager (psutil)
    instead of a PowerShell/CIM query. StartMode keeps the Win32_Service words ('Auto', 'Manual', 'Disabled')."""
    import psutil
    modes = {"automatic": "Auto", "manual": "Manual", "disabled": "Disabled"}
    out = {}
    for n in names:
        try:
            d = psutil.win_service_get(n).as_dict()
        except psutil.NoSuchProcess:
            continue
        out[d["name"]] = {"Name": d["name"], "State": "Running" if d["status"] == "running" else "Stopped",
                          "StartMode": modes.get(d["start_type"], "Manual")}
    return out


def services_list():
    names = [s for e in SERVICES for s in e["svc"]]
    try:
        st = _service_rows(names)
    except Exception:  # the service manager refused: ask Windows the old way
        flt = " OR ".join(f"Name='{n}'" for n in names)
        rows = ps_json(f"Get-CimInstance Win32_Service -Filter \"{flt}\" | Select-Object Name, State, StartMode", timeout=30, as_list=True)
        st = {r["Name"]: r for r in rows}
    printers = _printers()
    out = []
    for e in SERVICES:
        present = [st[s] for s in e["svc"] if s in st]
        if not present:
            continue
        modes = [_START.get(p.get("StartMode"), "manual") for p in present]
        cur = "auto" if "auto" in modes else "manual" if "manual" in modes else "disabled"
        it = {k: e[k] for k in ("id", "bal", "max") if k in e}
        it.update({"name": tr(e["name"]), "desc": tr(e["desc"])})
        it.update({"svc": [p["Name"] for p in present], "run": any(p.get("State") == "Running" for p in present), "start": cur,
                   "delayed": cur == "auto" and any(_delayed(p["Name"]) for p in present)})
        if e.get("warn"):
            it["warn"] = tr(e["warn"])
        if e.get("printers"):
            if printers:
                it["bal"] = None
                it["warn"] = tr("You have printers installed ({names}): leave it as it is.", names=", ".join(printers[:2]))
            else:
                it["desc"] += " " + tr("Your PC has no printers installed.")
                it["warn"] = tr("If you ever need to print, turn it back on.")
        out.append(it)
    return {"items": out, "admin": _admin()}


def _delayed(name):
    """True for an automatic service set to "Automatic (delayed start)": Win32_Service reports it as plain Auto."""
    return registry.get_value(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}", "DelayedAutoStart", V64) == 1


def _restore_service(svc, mode, delayed=False, was_running=False):
    """Puts one service back: start type (delayed-auto through sc.exe, which Set-Service can't express) and running state."""
    if mode == "auto" and delayed:
        code, out = rsys.run(["sc.exe", "config", svc, "start=", "delayed-auto"], timeout=40)
        ok = code == 0
    else:
        out = ps(f"Set-Service -Name '{svc}' -StartupType {_START_PS.get(mode, 'Manual')} -ErrorAction Stop; 'ok'", timeout=40)
        ok = "ok" in (out or "").split()
        if ok and mode == "auto":  # Set-Service doesn't clear a leftover delayed flag on every build
            registry_set_delayed(svc, False)
    if mode == "auto" or (was_running and mode != "disabled"):
        ps(f"Start-Service -Name '{svc}' -ErrorAction SilentlyContinue", timeout=40)
    return ok


def registry_set_delayed(svc, on):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{svc}", 0,
                            winreg.KEY_SET_VALUE | V64) as k:
            winreg.SetValueEx(k, "DelayedAutoStart", 0, winreg.REG_DWORD, 1 if on else 0)
    except OSError:
        pass


def services_apply(changes):
    """changes: {catalog_id: 'auto'|'manual'|'disabled'}. Saves the previous state (start type, delayed start and
    whether it was running) BEFORE touching anything, and returns its id for undo. If the backup can't be
    written, nothing is changed."""
    if not _admin():
        return _need_admin()
    cat = {e["id"]: e for e in SERVICES}
    before = ps_json("Get-CimInstance Win32_Service | Select-Object Name, StartMode, State", timeout=30, as_list=True)
    prev = {r["Name"]: r for r in before}
    backup = {"t": time.time(), "prev": {}, "delayed": [], "running": []}
    todo = []
    for cid, mode in changes.items():
        e = cat.get(cid)
        if not e or mode not in _START_PS:
            continue
        for svc in e["svc"]:
            if svc not in prev:
                continue
            row = prev[svc]
            backup["prev"][svc] = _START.get(row.get("StartMode"), "manual")
            if backup["prev"][svc] == "auto" and _delayed(svc):
                backup["delayed"].append(svc)
            if row.get("State") == "Running":
                backup["running"].append(svc)
            todo.append((svc, mode))
    if changes and not todo:   # nothing could be changed (Windows' list of services was not read): not a success
        return {"ok": False, "results": [], "backup": None, "error": tr("Couldn't read from Windows")}
    try:
        bid = unique_bid(os.path.join(DATA, "services"))
        write_json_atomic(os.path.join(DATA, "services", bid + ".json"), backup)
    except OSError as e:
        return {"ok": False, "results": [], "backup": None, "error": tr("Nothing was changed: the backup of the services couldn't be saved ({error}).", error=e)}
    results = []
    for svc, mode in todo:
        if mode == "auto":
            cmd = f"Set-Service -Name '{svc}' -StartupType Automatic -ErrorAction Stop"
        else:
            cmd = f"Set-Service -Name '{svc}' -StartupType {_START_PS[mode]} -ErrorAction Stop"
        if mode == "disabled":
            cmd += f"; Stop-Service -Name '{svc}' -Force -ErrorAction SilentlyContinue"
        out = ps(cmd + "; 'ok'", timeout=40)
        results.append({"svc": svc, "ok": "ok" in out.splitlines()[-1:] if out else False})
    return {"ok": all(r["ok"] for r in results), "results": results, "backup": bid}


def _services_undo(bid, say=None):
    if not _admin():
        return _need_admin()
    f = os.path.join(DATA, "services", os.path.basename(str(bid)) + ".json")
    with open(f, encoding="utf-8") as fh:
        data = json.load(fh)
    prev, delayed, running = data["prev"], set(data.get("delayed") or []), set(data.get("running") or [])
    items = list(prev.items())
    bad = []
    for n, (svc, mode) in enumerate(items):
        if say:
            say(n / max(1, len(items)), tr("Restoring {name} ({n} of {total})", name=svc, n=n + 1, total=len(items)))
        if not _restore_service(svc, mode, svc in delayed, svc in running):
            bad.append(svc)
    return {"ok": not bad, "restored": len(items) - len(bad), "failed": bad}


def services_undo(bid):
    return _services_undo(bid)


def services_undo_job(job, bid):
    """services_undo as a job: about a second per service, so the interface can say which one is being restored."""
    r = _services_undo(bid, lambda p, t: job.progress(p, t, force=True))
    job.progress(1, tr("Done"), force=True)
    return r


# =============================================================== registry
def _drive_ready(root):
    """True only for a local fixed drive that answers right now (not unplugged, not a locked BitLocker volume,
    not removable, not a network drive)."""
    try:
        if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
            return False
    except Exception:
        return False
    try:
        os.listdir(root)
        return True
    except OSError:
        return False


_SYS32 = re.compile(r"(?i)([\\/])system32(?=[\\/]|$)")


def _variants(p, view):
    """The paths a 32-bit program could mean (System32 is really SysWOW64 for it). 64-bit entries are checked as
    they are; 32-bit ones, and the user's (HKCU is shared by both), both ways. Program Files and Program Files (x86)
    (and their Common Files) are tried both ways for every entry: a WOW6432Node entry that says "C:\\Program
    Files\\Common Files\\..." may mean the x86 folder, and a file found in either one is not missing."""
    out = [p]
    if view != V64:
        w = _SYS32.sub(r"\1SysWOW64", p)
        if w != p:
            out.append(w)
    low = p.lower()
    for a, b in _pf_pairs():
        for x, y in ((a, b), (b, a)):
            if x and y and low.startswith(x.lower() + "\\"):
                out.append(y + p[len(x):])
    return out


def _pf_pairs():
    env = os.environ
    pf64 = env.get("ProgramW6432") or env.get("ProgramFiles") or r"C:\Program Files"
    pf86 = env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    cf64 = env.get("CommonProgramW6432") or os.path.join(pf64, "Common Files")
    cf86 = env.get("CommonProgramFiles(x86)") or os.path.join(pf86, "Common Files")
    return ((cf64.rstrip("\\"), cf86.rstrip("\\")), (pf64.rstrip("\\"), pf86.rstrip("\\")))


_INBOX_APPS = {"tabtip.exe"}   # the touch keyboard (RsHub itself opens it); Windows keeps its App Path


def _inbox_app_path(name, target):
    """True for an App Path that belongs to Windows itself: a known inbox app, or a target in the Windows folder,
    in Common Files\\microsoft shared, or in a Program Files folder named "Windows ..." or "Internet Explorer"."""
    if str(name or "").lower() in _INBOX_APPS:
        return True
    t = os.path.normcase(os.path.normpath(str(target or ""))) if target else ""
    if not t:
        return False
    win = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    (cf64, cf86), pfs = _pf_pairs()
    roots = [win, os.path.join(cf64, "microsoft shared"), os.path.join(cf86, "microsoft shared")]
    if any(t == os.path.normcase(r) or t.startswith(os.path.normcase(r) + "\\") for r in roots):
        return True
    for pf in pfs:
        pre = os.path.normcase(pf) + "\\"
        if t.startswith(pre):
            first = t[len(pre):].split("\\", 1)[0]
            if first.startswith("windows") or first == "internet explorer":
                return True
    return False


_PROTECTED_MSG = "Windows protects this entry; it was left as is."
_TI_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"   # NT SERVICE\TrustedInstaller


def _key_owner(hkey):
    """The owner SID (as text) of an open registry key, or None."""
    try:
        adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
        owner, sd, text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_wchar_p()
        # SE_REGISTRY_KEY = 4, OWNER_SECURITY_INFORMATION = 1
        if adv.GetSecurityInfo(ctypes.c_void_p(int(hkey)), 4, 1, ctypes.byref(owner), None, None, None,
                               ctypes.byref(sd)) != 0:
            return None
        try:
            if not adv.ConvertSidToStringSidW(owner, ctypes.byref(text)):
                return None
            try:
                return text.value
            finally:
                k32.LocalFree(text)
        finally:
            k32.LocalFree(sd)
    except Exception:  # noqa: BLE001
        return None


def _windows_protected(ref):
    """True when Windows keeps this registry entry to itself: the key is owned by TrustedInstaller, or (where this
    run could change it: the user's own keys, or any key as administrator) opening it with the rights the fix needs is
    refused. Reads only; a key that no longer exists is not protected."""
    root = registry.HIVES[ref.hive]
    try:
        with winreg.OpenKey(root, ref.path, 0, winreg.KEY_READ | ref.view) as k:
            if _key_owner(k) == _TI_SID:
                return True
    except FileNotFoundError:
        return False
    except OSError:
        return True   # not even readable
    if ref.hive == "HKLM" and not _admin():
        return False  # an ordinary user can't tell yet; the fix asks for administrator mode first
    need = winreg.KEY_SET_VALUE if ref.value is not None else winreg.KEY_ALL_ACCESS   # what registry_fix opens it with
    try:
        winreg.OpenKey(root, ref.path, 0, need | ref.view).Close()
    except PermissionError:
        return True
    except OSError:
        pass
    return False


def _definitely_missing(path, view=0, expand=True):
    """True only when `path` is surely gone. Unknown is never "missing": relative or UNC paths, drives that are
    not a ready local fixed drive (unplugged, locked, network, removable) and anything we can't look at (access
    denied) are left alone, so a healthy entry is never offered for deletion."""
    p = (path or "").strip().strip('"')
    if expand:
        if view != V64:  # a 32-bit program's %ProgramFiles% is the x86 folder
            for a, b in (("%ProgramFiles%", "%ProgramFiles(x86)%"), ("%CommonProgramFiles%", "%CommonProgramFiles(x86)%")):
                p32 = re.sub(re.escape(a), lambda m: b, p, flags=re.I)
                if p32 != p:
                    p = p32
                    break
        p = os.path.expandvars(p)
    if not p or p.startswith("\\\\") or p.startswith("//"):
        return False
    drive = os.path.splitdrive(p)[0]
    if len(drive) != 2 or not os.path.isabs(p):
        return False
    if not _drive_ready(drive + "\\"):
        return False
    for cand in _variants(p, view):
        try:
            os.stat(cand)
            return False
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return False
    return True


def _exists(p, view=0):
    """True when the path exists or we can't tell (so it's not treated as broken)."""
    return not _definitely_missing(p, view)


def _abs_missing(cmd, view=0):
    """The executable of a command if it is an absolute path that surely no longer exists; otherwise None."""
    from .removal.safety import image_path
    exe = image_path(cmd or "")
    if not exe or not os.path.isabs(exe):
        return None
    if exe.lower().endswith(("msiexec.exe", "rundll32.exe")):
        return None
    if not _definitely_missing(exe, view):
        return None
    # an unquoted command with no .exe/.dll/.sys is cut at its first space ("C:\Program Files\Foo\run.bat" gives
    # "C:\Program"): it is only broken when no longer reading of it exists either
    c = os.path.expandvars((cmd or "").strip())
    if not c.startswith('"') and not re.match(r"(.+?\.(exe|sys|dll))\b", c, re.I):
        words = c.split(" ")
        for i in range(2, len(words) + 1):
            if not _definitely_missing(" ".join(words[:i]), view):
                return None
    return exe


def registry_scan(job):
    """Looks for genuinely broken entries (pointing to files that no longer exist). Changes nothing."""
    groups = {}

    def add(g, ref, why):
        if _windows_protected(ref):   # an entry Windows keeps to itself could only fail to be fixed: not offered
            return
        groups.setdefault(g, []).append({"id": ref.to_str(), "k": ref.display(), "why": why})

    HK = winreg.HKEY_LOCAL_MACHINE
    # 1. broken uninstallers
    job.progress(.05, tr("Uninstallers"))
    for hive, root, view in (("HKLM", HK, V64), ("HKLM", HK, V32), ("HKCU", winreg.HKEY_CURRENT_USER, 0)):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        for sub in registry.subkeys(root, base, view):
            p = f"{base}\\{sub}"
            us = registry.get_value(root, p, "UninstallString", view)
            name = registry.get_value(root, p, "DisplayName", view)
            if not us or not name or registry.get_value(root, p, "SystemComponent", view) == 1:
                continue
            miss = _abs_missing(str(us), view)
            loc = registry.get_value(root, p, "InstallLocation", view)
            if miss and not (loc and _exists(str(loc), view)):
                add("Broken uninstallers", RegRef(hive, p, view), tr('"{name}": the uninstaller {path} no longer exists.', name=name, path=miss))
    # 2. App Paths
    job.progress(.25, tr("App paths"))
    for hive, root, view in (("HKLM", HK, V64), ("HKLM", HK, V32), ("HKCU", winreg.HKEY_CURRENT_USER, 0)):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for sub in registry.subkeys(root, base, view):
            p = f"{base}\\{sub}"
            d = registry.get_value(root, p, "", view)
            if not d:
                continue
            target = os.path.expandvars(str(d).strip().strip('"'))
            if _inbox_app_path(sub, target):   # Windows' own (the touch keyboard...): never offered
                continue
            if _definitely_missing(str(d), view):
                add("Broken app paths", RegRef(hive, p, view), tr("Points to {path}, which no longer exists.", path=target))
    # 3. startup entries that point to nothing
    job.progress(.45, tr("Startup"))
    for hive, root, view in (("HKCU", winreg.HKEY_CURRENT_USER, 0), ("HKLM", HK, V64), ("HKLM", HK, V32)):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
        for name, data, _ in registry.values(root, base, view):
            miss = _abs_missing(str(data), view)
            if miss:
                add("Startup entries pointing nowhere", RegRef(hive, base, view, name), tr("Starts {path}, which doesn't exist.", path=miss))
    # 4. shared files that no longer exist
    job.progress(.6, tr("Shared files"))
    for view in (V64, V32):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs"
        for name, data, _ in registry.values(HK, base, view):
            if _definitely_missing(name, view, expand=False):
                add("Missing shared files", RegRef("HKLM", base, view, name), tr("The file no longer exists."))
    # 5. orphaned file types (the user's only)
    job.progress(.75, tr("File types"))
    cu, cls = winreg.HKEY_CURRENT_USER, r"Software\Classes"
    for sub in registry.subkeys(cu, cls):
        if not sub.startswith("."):
            continue
        prog = registry.get_value(cu, f"{cls}\\{sub}", "")
        if not prog:
            continue
        in_cu = registry.key_exists(RegRef("HKCU", f"{cls}\\{prog}"))
        in_lm = registry.key_exists(RegRef("HKLM", f"SOFTWARE\\Classes\\{prog}"))
        if not in_cu and not in_lm and not registry.subkeys(cu, f"{cls}\\{sub}"):
            add("Orphaned file types", RegRef("HKCU", f"{cls}\\{sub}"), tr('Says it opens with "{name}", which no longer exists.', name=prog))
    for sub in registry.subkeys(cu, cls):
        if sub.startswith(".") or sub.startswith("{"):
            continue
        cmd = registry.get_value(cu, f"{cls}\\{sub}\\shell\\open\\command", "")
        miss = _abs_missing(str(cmd), 0) if cmd else None
        if miss:
            add("Orphaned file types", RegRef("HKCU", f"{cls}\\{sub}"), tr("Class of a program that is gone ({path}).", path=miss))
    # 6. broken context menu entries
    job.progress(.88, tr("Context menu"))
    for scope in ("*", "Directory", r"Directory\Background", "Folder"):
        for hive, root, base in (("HKLM", HK, r"SOFTWARE\Classes"), ("HKCU", cu, r"Software\Classes")):
            hp = f"{base}\\{scope}\\shellex\\ContextMenuHandlers"
            for h in registry.subkeys(root, hp):
                clsid = registry.get_value(root, f"{hp}\\{h}", "") or (h if h.startswith("{") else "")
                if not clsid or not clsid.startswith("{"):
                    continue
                dll, dview = None, 0
                for vroot, vbase, vview in ((HK, r"SOFTWARE\Classes", V64), (cu, r"Software\Classes", 0), (HK, r"SOFTWARE\Classes", V32)):
                    dll = registry.get_value(vroot, f"{vbase}\\CLSID\\{clsid}\\InprocServer32", "", vview)
                    if dll:
                        dview = vview
                        break
                if not dll:
                    continue
                path = os.path.expandvars(str(dll).strip('"'))
                if _definitely_missing(str(dll), dview):
                    add("Broken context menu entries", RegRef(hive, f"{hp}\\{h}"), tr('Adds "{name}" to the right-click menu, but {file} no longer exists.', name=h, file=os.path.basename(path)))
    job.progress(1, tr("Done"))
    order = ["Broken uninstallers", "Broken app paths", "Orphaned file types", "Startup entries pointing nowhere",
             "Missing shared files", "Broken context menu entries"]
    return [{"g": tr(g), "items": groups[g]} for g in order if g in groups]


def _enc(d):
    return {"hex": bytes(d).hex()} if isinstance(d, (bytes, bytearray)) else d


def _dec(d):
    return bytes.fromhex(d["hex"]) if isinstance(d, dict) and "hex" in d else d


def _reg_root(ref):
    """Where a registry backup goes: HKLM keys (elevated runs only) in ProgramData, the user's own in their profile."""
    return DATA if ref.hive == "HKLM" else USER_DATA


def registry_backups_exist():
    """True if this PC already has a registry-fix backup (machine-wide or the user's own). Read-only. The UI uses it
    once, when the `registryCleaner` setting does not exist yet: someone who already used the cleaner keeps it on."""
    for root in (DATA, USER_DATA):
        try:
            if any(os.path.isdir(os.path.join(root, "registry", n)) for n in os.listdir(os.path.join(root, "registry"))):
                return True
        except OSError:
            continue
    return False


def registry_fix(job, ids):
    """Backs up and then deletes each broken entry. A single value is saved as (name, type, data) and put back
    exactly like that; only when a whole key goes is the key exported to .reg. Each backup is written to disk
    BEFORE its entry is deleted: if it can't be written nothing else is touched. Returns the backup id (undo)."""
    refs = [RegRef.from_str(i) for i in ids]
    if any(user_needs_admin(r.hive, r.path) for r in refs) and not _admin():
        return _need_admin()
    roots = sorted({_reg_root(r) for r in refs})
    try:
        bid = unique_bid(*[os.path.join(r, "registry") for r in roots])
    except OSError as e:
        return {"fixed": 0, "failed": [tr("Nothing was changed: the backup couldn't be saved ({error}).", error=e)], "backup": None, "folder": None}
    sess = {}  # root -> [folder, manifest]
    fixed, failed, n = 0, [], 0
    abort = None
    for ref in refs:
        job.check()
        job.progress(n / max(1, len(refs)), ref.display())
        n += 1
        if abort:
            failed.append(f"{ref.display()}: {abort}")
            continue
        if _windows_protected(ref):   # it would only fail (access denied), maybe halfway: left as is, said plainly
            failed.append(f"{ref.display()}: {tr(_PROTECTED_MSG)}")
            continue
        root = _reg_root(ref)
        if root not in sess:
            sess[root] = [os.path.join(root, "registry", bid), []]
        folder, man = sess[root]
        mf = os.path.join(folder, "manifest.json")
        appended = False
        try:
            os.makedirs(folder, exist_ok=True)
            if ref.value is not None:
                cur = registry.read_value_full(ref)
                if cur is None:
                    continue  # already gone: nothing to delete, nothing to restore
                entry = {"kind": "value", "hive": ref.hive, "path": ref.path, "view": ref.view, "name": ref.value,
                         "type": cur[1], "data": _enc(cur[0]), "k": ref.display()}
            else:
                f = os.path.join(folder, f"{len(man):03d}.reg")
                if not registry.export_key(ref, f):
                    failed.append(f"{ref.display()}: {tr('the key could not be saved')}")
                    continue
                entry = {"kind": "key", "file": f, "view": ref.view, "k": ref.display()}
            man.append(entry)
            appended = True
            write_json_atomic(mf, man)
        except OSError as e:
            if appended:
                man.pop()
            abort = tr("not changed, its backup couldn't be saved ({error})", error=e)
            failed.append(f"{ref.display()}: {abort}")
            continue
        try:
            if ref.value is not None:
                registry.delete_value(ref)
            else:
                registry.delete_tree(ref)
            fixed += 1
        except OSError as e:
            gone = man.pop()
            if gone.get("kind") == "key":   # some subkeys may already be gone: put the whole key back from its .reg
                registry.import_reg(gone["file"], gone.get("view", 0))
            try:
                write_json_atomic(mf, man)
            except OSError:
                pass
            denied = isinstance(e, PermissionError) or getattr(e, "winerror", None) == 5
            failed.append(f"{ref.display()}: {tr(_PROTECTED_MSG) if denied else e}")
    for root, (folder, man) in list(sess.items()):
        if not man:  # nothing was saved there: don't leave an empty backup
            shutil.rmtree(folder, ignore_errors=True)
            del sess[root]
    job.progress(1, tr("Done"))
    folder = sess[DATA][0] if DATA in sess else (next(iter(sess.values()))[0] if sess else None)
    return {"fixed": fixed, "failed": failed, "backup": bid, "folder": folder}


def _registry_undo(bid, say=None):
    bid = os.path.basename(str(bid))
    man, folders = [], []
    for root in (DATA, USER_DATA):
        folder = os.path.join(root, "registry", bid)
        mf = os.path.join(folder, "manifest.json")
        if os.path.isfile(mf):
            with open(mf, encoding="utf-8") as fh:
                man += json.load(fh)
            folders.append(folder)
    if not folders:
        raise FileNotFoundError(tr("That backup is gone."))
    if any(m["k"].upper().startswith("HKLM") for m in man) and not _admin():
        return _need_admin()
    ok = 0
    for n, m in enumerate(man):
        if say:
            say(n / max(1, len(man)), tr("Restoring registry entries ({n} of {total})", n=n + 1, total=len(man)))
        if m.get("kind") == "value":
            try:
                registry.set_value(RegRef(m["hive"], m["path"], m.get("view", 0), m["name"]), _dec(m["data"]), m["type"])
                ok += 1
            except OSError:
                pass
        else:
            r, _ = registry.import_reg(m["file"], m.get("view", 0))
            ok += bool(r)
    return {"ok": ok == len(man), "restored": ok, "folder": folders[0]}


def registry_undo(bid):
    return _registry_undo(bid)


def registry_undo_job(job, bid):
    """registry_undo as a job, with one progress step per entry imported again."""
    r = _registry_undo(bid, lambda p, t: job.progress(p, t, force=True))
    job.progress(1, tr("Done"), force=True)
    return r


# =============================================================== repair
def _oem_codec():
    """The console code page (what chkdsk and DISM write when their output is redirected)."""
    try:
        cp = ctypes.windll.kernel32.GetOEMCP()
        codecs.lookup(f"cp{cp}")
        return f"cp{cp}"
    except Exception:
        return "cp850"


_PCT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
_BAR_ONLY = re.compile(r"[\[\]=\-\s\d.,%]+")


def parse_pct(text):
    """Percentage (0..1) in a console line, or None. With several (chkdsk: “Stage: 40%; Total: 12%”) the last one
    is the overall one. Works in any language: only the number and the % sign matter."""
    found = _PCT.findall(text or "")
    if not found:
        return None
    try:
        v = float(found[-1].replace(",", "."))
    except ValueError:
        return None
    return v / 100 if 0 <= v <= 100 else None


def is_bar(text):
    """DISM-style progress bars ("[=====   38.4%   ]"): they only carry the %."""
    return bool(text) and "%" in text and _BAR_ONLY.fullmatch(text) is not None


class ConsoleStream:
    """Turns the raw bytes of a console program into lines, as they arrive.

    - Encoding: SFC writes UTF-16 when its output is redirected (with or without a BOM); chkdsk and DISM use the
      console code page. It's decided from the first bytes and decoded incrementally (a character split between
      two reads is fine).
    - "\\r" without "\\n" rewrites the same line (SFC's "Verification 41% complete.", DISM's bar, chkdsk's
      "Progress: … Total: 12%"): those come out as ("live", text); finished lines as ("line", text).
    """

    def __init__(self, oem="cp850"):
        self.oem = oem
        self.dec = None
        self.head = b""
        self.cur = ""
        self.cr = False  # the last character seen was a lone \r (it may be the first half of \r\n)
        self.encoding = None

    def _pick(self, data):
        if data.startswith(b"\xff\xfe"):
            return "utf-16"  # the BOM is consumed by the decoder
        # console code pages never contain NUL bytes; UTF-16 text has them (ASCII, \r\n, many CJK characters)
        return "utf-16-le" if b"\x00" in data[:64] else self.oem

    def feed(self, chunk):
        if self.dec is None:
            self.head += chunk
            if len(self.head) < 16 and not self.head.startswith(b"\xff\xfe"):
                return []
            self.encoding = self._pick(self.head)
            self.dec = codecs.getincrementaldecoder(self.encoding)(errors="replace")
            chunk, self.head = self.head, b""
        return self._text(self.dec.decode(chunk))

    def close(self):
        ev = []
        if self.dec is None and self.head:
            self.encoding = self._pick(self.head)
            self.dec = codecs.getincrementaldecoder(self.encoding)(errors="replace")
            ev += self._text(self.dec.decode(self.head))
        elif self.dec is not None:
            ev += self._text(self.dec.decode(b"", final=True))
        if self.cr:
            self.cr = False
            ev.append(("live", self.cur.strip()))
            self.cur = ""
        if self.cur.strip():
            ev.append(("line", self.cur.strip()))
        self.cur = ""
        return [e for e in ev if e[1]]

    def peek(self):
        """The line being written right now (no \\r or \\n yet), e.g. a % that hasn't been rewritten yet."""
        return self.cur.strip()

    def _text(self, s):
        ev = []
        for ch in s.replace("\x00", ""):
            if self.cr:
                self.cr = False
                if ch == "\n":  # \r\n: an ordinary end of line
                    ev.append(("line", self.cur.strip()))
                    self.cur = ""
                    continue
                ev.append(("live", self.cur.strip()))  # lone \r: the line will be rewritten
                self.cur = ""
            if ch == "\r":
                self.cr = True
            elif ch == "\n":
                ev.append(("line", self.cur.strip()))
                self.cur = ""
            else:
                self.cur += ch
        return [e for e in ev if e[1]]


def _cbs_tail(state):
    """Last “[SR]” line of CBS.log, where SFC writes while it works (when its own output is quiet)."""
    f = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Logs", "CBS", "CBS.log")
    try:
        size = os.path.getsize(f)
        pos = state.get("pos")
        if pos is None or pos > size:
            pos = max(0, size - 65536)
        with open(f, "rb") as fh:
            fh.seek(pos)
            data = fh.read(min(size - pos, 1 << 20))
        state["pos"] = pos + len(data)
        lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if "[SR]" in ln]
        if lines:
            state["last"] = lines[-1].split("[SR]", 1)[1].strip()[:140]
    except OSError:
        pass
    return state.get("last")


def _stream(job, cmd, weight=(0.0, 1.0), tool=None):
    """Runs a command showing its output live: finished lines go to the log, lines rewritten with \\r (the % of
    SFC, DISM and chkdsk) update the progress bar and a "live" line. A heartbeat every second sends the elapsed
    time and how long the program has been quiet, so the UI can say it's still working."""
    job.log("> " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=NO_WINDOW, shell=isinstance(cmd, str))
    cs = ConsoleStream(_oem_codec())
    lo, hi = weight
    t0 = time.monotonic()
    st = {"out": t0, "live": "", "last": "", "pct": None, "done": False}
    cbs = {}
    lock = threading.Lock()

    def heartbeat():
        while not st["done"]:
            time.sleep(1)
            if st["done"]:
                break
            if job.cancelled:
                try:
                    p.kill()
                except OSError:
                    pass
            now = time.monotonic()
            with lock:
                live = cs.peek() or st["live"]
            quiet = now - st["out"]
            extra = {}
            pct = parse_pct(live)
            if pct is not None and hi > lo:
                st["pct"] = pct
                extra["pct"] = lo + (hi - lo) * pct
            if tool == "sfc" and quiet > 8:
                act = _cbs_tail(cbs)
                if act:
                    extra["act"] = act
            job.progress(extra.pop("pct", None), None, force=True, el=round(now - t0), quiet=round(quiet), live=live,
                         hasPct=st["pct"] is not None, **extra)
    hb = threading.Thread(target=heartbeat, daemon=True)
    if weight != (0, 0):
        hb.start()

    def handle(events):
        for kind, text in events:
            st["out"] = time.monotonic()
            pct = parse_pct(text)
            if pct is not None and hi > lo:
                st["pct"] = pct
                job.progress(lo + (hi - lo) * pct, text, live=text if kind == "live" else "", hasPct=True,
                             el=round(time.monotonic() - t0), quiet=0)
            if kind == "live":
                st["live"] = text
                continue
            if is_bar(text) or text == st["last"]:
                continue
            st["last"] = text
            job.log(text)

    try:
        while True:
            chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
            if not chunk:
                break
            with lock:
                ev = cs.feed(chunk)
            handle(ev)
        with lock:
            ev = cs.close()
        handle(ev)
    finally:
        st["done"] = True
    code = p.wait()
    if job.cancelled:
        job.stop()
    return code


def _restart_explorer(job):
    subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], capture_output=True, creationflags=NO_WINDOW)
    job.log(tr("File Explorer closed."))
    for _ in range(12):  # Windows reopens it by itself (AutoRestartShell)
        time.sleep(.5)
        if any(p["name"].lower() == "explorer.exe" for p in rsys.list_processes()):
            job.log(tr("File Explorer is open again."))
            return
    # if it didn't come back: open it with a non-elevated token (opening it from an admin process would leave it elevated)
    subprocess.Popen(["runas", "/trustlevel:0x20000", "explorer.exe"], creationflags=NO_WINDOW)
    job.log(tr("File Explorer is open again."))


REPAIR_ADMIN = {"sfc", "dism", "chkdsk", "wu", "net"}
# Repairs that erase something the user configured: never from an unattended routine.
REPAIR_ATTENDED_ONLY = {"net"}
WU_SERVICES = ["wuauserv", "cryptSvc", "bits", "msiserver"]


def _svc_running(name):
    try:
        import psutil
        return psutil.win_service_get(name).status() == "running"
    except Exception:
        return False


def _prune_old(job, base):
    """Keeps only the newest <base>.old-* folder (the one just made); older ones are deleted."""
    olds = sorted(d for d in glob.glob(base + ".old-*") if os.path.isdir(d))
    for d in olds[:-1]:
        job.check()
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.exists(d):
            job.log(tr("Deleted the older copy {name}", name=os.path.basename(d)))


def _repair_wu(job, win):
    """Stops the Windows Update services, renames its caches and starts the services again. The services are
    always started again (also when it is cancelled or something fails half way) and only the newest renamed
    copy of each folder is kept."""
    was = {s: _svc_running(s) for s in WU_SERVICES}
    touched = []
    try:
        for i, s in enumerate(WU_SERVICES):
            job.progress(.05 + i * .08, tr("Stopping {name}", name=s))
            touched.append(s)
            _stream(job, ["net", "stop", s, "/y"], weight=(0, 0))
        stamp = time.strftime("%Y%m%d%H%M%S")
        for i, d in enumerate([os.path.join(win, "SoftwareDistribution"), os.path.join(win, "System32", "catroot2")]):
            job.progress(.45 + i * .12, tr("Renaming {name}", name=os.path.basename(d)))
            try:
                os.rename(d, f"{d}.old-{stamp}")
                job.log(f"{d} → {os.path.basename(d)}.old-{stamp}")
            except OSError as e:
                job.log(tr("Couldn't rename {path}: {error}", path=d, error=e))
            _prune_old(job, d)
    finally:
        # not _stream: once cancelled it would raise again before the services are back
        for s in reversed(touched):
            if s == "msiserver" and not was.get(s):
                continue  # on demand; it wasn't running
            code, _ = rsys.run(["net", "start", s], timeout=60)
            job.log(tr("Starting {name}", name=s))
    job.log(tr("Windows Update is ready to search again."))
    return 0


def _save_network_config(root=None):
    """Saves every adapter's IP/DNS configuration before a network reset: a netsh script that puts it back
    (`netsh -f restore.netsh`, as administrator), the plain-text listing and the structured data. Returns the
    folder. Raises OSError when nothing usable was saved, so the reset is not run."""
    root = root or USER_DATA
    folder = os.path.join(root, "network", new_bid())
    os.makedirs(folder, exist_ok=True)
    code, dump = rsys.run(["netsh", "interface", "ip", "dump"], timeout=60)
    code2, show = rsys.run(["netsh", "interface", "ip", "show", "config"], timeout=60)
    if code != 0 or not (dump or "").strip() or code2 != 0 or not (show or "").strip():
        shutil.rmtree(folder, ignore_errors=True)
        raise OSError(tr("Windows didn't give the network configuration"))
    try:
        info = ps_json("Get-NetIPConfiguration -All | ForEach-Object { [pscustomobject]@{ alias=$_.InterfaceAlias; "
                       "index=$_.InterfaceIndex; ipv4=@($_.IPv4Address | ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" }); "
                       "gateway=@($_.IPv4DefaultGateway | ForEach-Object { $_.NextHop }); "
                       "dns=@($_.DNSServer | ForEach-Object { $_.ServerAddresses }); dhcp=[string]$_.NetIPv4Interface.Dhcp } }",
                       timeout=40, as_list=True)
    except Exception:
        info = []
    with open(os.path.join(folder, "restore.netsh"), "w", encoding="utf-8") as fh:
        fh.write(dump)
    with open(os.path.join(folder, "adapters.txt"), "w", encoding="utf-8") as fh:
        fh.write(show)
    write_json_atomic(os.path.join(folder, "adapters.json"), {"t": time.time(), "adapters": info})
    return folder


def repair(job, rid, unattended=False):
    """unattended: called from an automatic routine. Repairs that erase user settings ("net") refuse to run then;
    a job wrapped by Autopilot (it has a `parent`) counts as unattended even if the flag is not passed."""
    if unattended or getattr(job, "parent", None) is not None:
        if rid in REPAIR_ATTENDED_ONLY:
            raise PermissionError(tr("The network reset erases static IP and DNS settings, so it only runs when you start it yourself from Super Optimizer."))
    if rid in REPAIR_ADMIN and not _admin():
        return _need_admin()
    win = os.environ.get("WINDIR", r"C:\Windows")
    extra = {}
    if rid == "sfc":
        code = _stream(job, ["sfc", "/scannow"], tool="sfc")
    elif rid == "dism":
        code = _stream(job, ["DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"], tool="dism")
    elif rid == "chkdsk":
        drive = os.environ.get("SystemDrive") or "C:"
        code = _stream(job, ["chkdsk", drive, "/scan"], tool="chkdsk")
    elif rid == "wu":
        code = _repair_wu(job, win)
    elif rid == "net":
        job.progress(.05, tr("Saving your network settings"))
        try:
            folder = _save_network_config()
        except OSError as e:
            job.log(tr("Couldn't save your network settings ({error}). The reset was cancelled.", error=e))
            job.progress(1, tr("Done"))
            return {"code": 1, "error": tr("Couldn't save your network settings ({error}). The reset was cancelled.", error=e)}
        msg = tr("Your network settings (static IP, DNS) were saved in {path}. To put them back, run restore.netsh from there as administrator.", path=folder)
        job.log(msg)
        extra = {"backup": folder, "msg": msg}
        job.progress(.1, "DNS")
        _stream(job, ["ipconfig", "/flushdns"], weight=(0, 0))
        job.progress(.4, "Winsock")
        _stream(job, ["netsh", "winsock", "reset"], weight=(0, 0))
        job.progress(.7, "TCP/IP")
        _stream(job, ["netsh", "int", "ip", "reset"], weight=(0, 0))
        job.log(tr("Done. Restart the PC to finish."))
        code = 0
    elif rid == "icons":
        local = os.environ.get("LOCALAPPDATA", "")
        job.progress(.1, tr("Closing File Explorer"))
        subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], capture_output=True, creationflags=NO_WINDOW)
        time.sleep(.6)
        n = 0
        for pat in (os.path.join(local, "Microsoft", "Windows", "Explorer", "iconcache_*.db"),
                    os.path.join(local, "Microsoft", "Windows", "Explorer", "thumbcache_*.db"), os.path.join(local, "IconCache.db")):
            for f in glob.glob(pat):
                try:
                    os.remove(f)
                    n += 1
                except OSError:
                    pass
        job.log(tr("{n} icon cache files deleted.", n=n))
        job.progress(.55, tr("Font cache"))
        if _admin():
            _stream(job, ["net", "stop", "FontCache", "/y"], weight=(0, 0))
            fc = os.path.join(win, "ServiceProfiles", "LocalService", "AppData", "Local", "FontCache")
            for f in glob.glob(os.path.join(fc, "*FontCache*")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            _stream(job, ["net", "start", "FontCache"], weight=(0, 0))
        else:
            job.log(tr("The font cache needs administrator mode: skipped."))
        subprocess.run(["ie4uinit.exe", "-show"], capture_output=True, creationflags=NO_WINDOW)
        job.progress(.85, tr("Opening File Explorer"))
        _restart_explorer(job)
        code = 0
    elif rid == "explorer":
        job.progress(.3, tr("Restarting File Explorer"))
        _restart_explorer(job)
        code = 0
    else:
        raise ValueError(rid)
    job.progress(1, tr("Done"))
    return {"code": code, **extra}


def restore_point(job, desc="RsHub, before optimizing"):
    if not _admin():
        return _need_admin()
    job.progress(.1, tr("Creating the restore point…"))
    ok, out = rsys.create_restore_point(desc)
    job.progress(1, tr("Done"))
    return {"ok": ok, "msg": out[-300:]}


# =============================================================== performance
_OVERLAY = {"961cc777-2547-4f9d-8174-7d86181b8a7a": "efficiency", "00000000-0000-0000-0000-000000000000": "balanced",
            "ded574b5-45a0-4f42-8737-46345c09c238": "best", "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "efficiency"}
_OVERLAY_GUID = {"efficiency": "961cc777-2547-4f9d-8174-7d86181b8a7a", "balanced": "00000000-0000-0000-0000-000000000000",
                 "best": "ded574b5-45a0-4f42-8737-46345c09c238"}
_PS_KEY = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
_VIS = {0: "auto", 1: "appearance", 2: "performance", 3: "custom"}


def _overlay(name):
    v = registry.get_value(winreg.HKEY_LOCAL_MACHINE, _PS_KEY, name)
    return _OVERLAY.get(str(v).lower(), "balanced") if v else "balanced"


def _plans():
    (code, out), (code2, a) = gather(lambda: rsys.run(["powercfg", "/list"]), lambda: rsys.run(["powercfg", "/a"]))
    plans = []
    for m in re.finditer(r"([0-9a-f]{8}-[0-9a-f-]{27})\s+\(([^)]+)\)\s*(\*)?", out, re.I):
        plans.append({"guid": m.group(1), "name": m.group(2), "on": bool(m.group(3))})
    standby = "S0" in a.split("\n\n")[0] if a else False
    return plans, standby


def _bg_apps():
    base = r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications"
    names = {}
    for a in _start_apps():
        if "!" in (a.get("AppID") or ""):
            names.setdefault(a["AppID"].split("!")[0], a["Name"])
    out = []
    for pfn in registry.subkeys(winreg.HKEY_CURRENT_USER, base):
        if pfn not in names:
            continue
        p = f"{base}\\{pfn}"
        dbu = registry.get_value(winreg.HKEY_CURRENT_USER, p, "DisabledByUser")
        dis = registry.get_value(winreg.HKEY_CURRENT_USER, p, "Disabled")
        v = "never" if dbu == 1 or dis == 1 else ("always" if dbu == 0 and dis == 0 else "opt")
        out.append({"id": pfn, "n": names[pfn], "v": v})
    out.sort(key=lambda x: x["n"].lower())
    return out


def perf_read():
    # powercfg (twice) and the Start menu apps (PowerShell) are independent: read them at the same time
    (plans, standby), _apps = gather(_plans, _start_apps)
    gm = registry.get_value(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\GameBar", "AutoGameModeEnabled")
    hags = registry.get_value(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode")
    vis = registry.get_value(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "VisualFXSetting")
    return {"ac": _overlay("ActiveOverlayAcPowerScheme"), "dc": _overlay("ActiveOverlayDcPowerScheme"),
            "plans": plans, "standby": standby, "visual": _VIS.get(vis if isinstance(vis, int) else 0, "auto"),
            "gameMode": gm != 0, "hags": hags == 2 if hags is not None else None, "bg": _bg_apps(), "admin": _admin()}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    import uuid
    u = uuid.UUID(s)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


def _plugged():
    import psutil
    b = psutil.sensors_battery()
    return True if b is None else bool(b.power_plugged)


def _apply_visual(mode):
    spi = ctypes.windll.user32.SystemParametersInfoW
    on = mode != "performance"

    class ANIMATIONINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("iMinAnimate", ctypes.c_int)]
    ai = ANIMATIONINFO(ctypes.sizeof(ANIMATIONINFO), 1 if on else 0)
    spi(0x0049, ai.cbSize, ctypes.byref(ai), 3)  # SPI_SETANIMATION
    for code in (0x1043, 0x1025, 0x1003, 0x1017, 0x1005, 0x1007, 0x101B):  # client areas, shadows, menus, tooltips, combo boxes, lists, cursor
        spi(code, 0, ctypes.c_void_p(1 if on else 0), 3)


def perf_set(key, value):
    """Changes a performance setting in the same place Windows stores it. Returns the state read back again."""
    prev = perf_read()
    if key in ("ac", "dc"):
        if value not in _OVERLAY_GUID:
            raise ValueError(value)
        current_src = "ac" if _plugged() else "dc"
        if key == current_src:
            r = ctypes.windll.powrprof.PowerSetActiveOverlayScheme(ctypes.byref(_guid(_OVERLAY_GUID[value])))
            if r != 0:
                raise OSError(tr("Windows didn't accept the mode (code {code})", code=r))
        else:
            if not _admin():
                return _need_admin()
            name = "ActiveOverlayAcPowerScheme" if key == "ac" else "ActiveOverlayDcPowerScheme"
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, _PS_KEY, 0, winreg.KEY_SET_VALUE | V64) as k:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, _OVERLAY_GUID[value])
    elif key == "visual":
        inv = {v: k for k, v in _VIS.items()}
        if value not in inv:
            raise ValueError(value)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "VisualFXSetting", 0, winreg.REG_DWORD, inv[value])
        _apply_visual(value)
    elif key == "gameMode":
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\GameBar", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "AutoGameModeEnabled", 0, winreg.REG_DWORD, 1 if value else 0)
    elif key == "hags":
        if not _admin():
            return _need_admin()
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", 0, winreg.KEY_SET_VALUE | V64) as k:
            winreg.SetValueEx(k, "HwSchMode", 0, winreg.REG_DWORD, 2 if value else 1)
    else:
        raise ValueError(key)
    now = perf_read()
    now["prev"] = {key: prev.get(key)}
    return now


def power_plan_set(guid):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", guid or ""):
        raise ValueError(guid)
    code, out = rsys.run(["powercfg", "/setactive", guid])
    if code != 0:
        raise OSError(out.strip()[:200])
    return perf_read()


def bg_set(pfn, mode):
    base = rf"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications\{pfn}"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_SET_VALUE) as k:
        if mode == "never":
            winreg.SetValueEx(k, "DisabledByUser", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "Disabled", 0, winreg.REG_DWORD, 1)
        elif mode == "always":
            winreg.SetValueEx(k, "DisabledByUser", 0, winreg.REG_DWORD, 0)
            winreg.SetValueEx(k, "Disabled", 0, winreg.REG_DWORD, 0)
        elif mode == "opt":
            for n in ("DisabledByUser", "Disabled"):
                try:
                    winreg.DeleteValue(k, n)
                except OSError:
                    pass
        else:
            raise ValueError(mode)
    return {"ok": True}

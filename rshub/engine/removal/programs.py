"""Inventory of installed programs (Win32 + Microsoft Store)."""
import ctypes
import datetime
import json
import os
import re
import subprocess
import winreg
import xml.etree.ElementTree as ET

from . import paths, registry, safety
from .paths import norm_path
from .registry import V32, V64, RegRef

NOWIN = 0x08000000
UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
SOURCES = [("HKLM", V64), ("HKLM", V32), ("HKCU", 0)]


# ---------------------------------------------------------------- MSI
def msi_info(code, prop):
    try:
        buf = ctypes.create_unicode_buffer(2048)
        n = ctypes.c_uint(2048)
        r = ctypes.windll.msi.MsiGetProductInfoW(code, prop, buf, ctypes.byref(n))
        return buf.value if r == 0 else None
    except Exception:
        return None


def msi_installed(code):
    """True if Windows Installer still has the product installed."""
    try:
        return ctypes.windll.msi.MsiQueryProductStateW(code) == 5  # INSTALLSTATE_DEFAULT
    except Exception:
        return False


def pack_guid(code):
    """MSI GUID to the 'packed' format used by Classes\\Installer\\Products."""
    g = (code or "").strip("{}").replace("-", "")
    if len(g) != 32:
        return None
    out = g[0:8][::-1] + g[8:12][::-1] + g[12:16][::-1]
    rest = g[16:]
    out += "".join(rest[i:i + 2][::-1] for i in range(0, 16, 2))
    return out.upper()


# ---------------------------------------------------------------- installer type
def exe_from_command(cmd):
    cmd = os.path.expandvars((cmd or "").strip())
    if not cmd:
        return None
    if cmd.startswith('"'):
        return cmd[1:].split('"', 1)[0]
    m = re.match(r"(.+?\.exe)\b", cmd, re.I)
    return m.group(1) if m else cmd.split(" ")[0]


def _is_nsis(exe):
    try:
        if not exe or not os.path.isfile(exe) or os.path.getsize(exe) > 40 * 1024 * 1024:
            return False
        with open(exe, "rb") as f:
            data = f.read()
        return b"Nullsoft" in data
    except OSError:
        return False


def detect_kind(p):
    u = (p.get("uninstall") or "").lower()
    key = (p.get("keyname") or "").lower()
    if p.get("windows_installer") or "msiexec" in u:
        return "msi"
    if key.endswith("_is1") or re.search(r"unins\d{3}\.exe", u):
        return "inno"
    if "winget" in u or "winget" in key:
        return "winget"
    if "officeclicktorun" in u:
        return "c2r"
    if re.search(r"update\.exe.*--uninstall", u):
        return "squirrel"
    if "steam://uninstall" in u:
        return "steam"
    if "package cache" in u and "/uninstall" in u:
        return "burn"
    if re.search(r"setup\.exe\"?\s.*--uninstall", u):
        return "chromium"
    if "installshield installation information" in u:
        return "installshield"
    exe = exe_from_command(p.get("uninstall")) or ""
    if re.search(r"\\(7-zip|winrar)\\uninstall\.exe$", exe, re.I):
        return "nsis"  # they accept /S just like NSIS
    if re.search(r"(uninst|unins|uninstall|remove)[^\\]*\.exe$", exe, re.I) and _is_nsis(exe):
        return "nsis"
    return "other"


KIND_LABEL = {"msi": "Windows Installer", "inno": "Inno Setup", "nsis": "NSIS", "winget": "WinGet",
              "c2r": "Click-to-Run", "squirrel": "Squirrel", "other": "Built into the program",
              "store": "Microsoft Store", "steam": "Steam", "burn": "WiX Burn", "chromium": "Chromium",
              "installshield": "InstallShield"}
# Silent switches per installer family (the same ones Bulk Crap Uninstaller uses). Squirrel (Discord, Teams
# classic, Slack...): "Update.exe --uninstall -s".
SILENT = {"inno": "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART", "nsis": "/S", "burn": "/quiet /norestart",
          "chromium": "--force-uninstall", "squirrel": "-s"}


def uninstall_command(p, quiet=False):
    """Command to run. Return (command | None, is_quiet)."""
    kind = p.get("kind")
    if kind == "msi" and p.get("msi_code"):
        return f'msiexec.exe /x {p["msi_code"]}' + (" /qn /norestart" if quiet else ""), quiet
    base = (p.get("uninstall") or "").strip()
    if quiet and p.get("quiet"):
        return p["quiet"].strip(), True
    if not base:
        return None, False
    if quiet and kind in SILENT:
        return f"{base} {SILENT[kind]}", True
    if quiet and kind == "winget" and "--silent" not in base.lower():
        return base + " --silent", True
    return base, False


def can_quiet(p):
    return bool(p.get("quiet")) or p.get("kind") in ("msi", "inno", "nsis", "winget", "burn", "chromium", "squirrel")


# ---------------------------------------------------------------- Win32 inventory
def _key_date(k):
    try:
        ts = winreg.QueryInfoKey(k)[2]
        return (datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ts // 10)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _fmt_date(s, fallback):
    s = str(s or "")
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return fallback


_BAD_LOC = re.compile(r"\\(uninstall(er)?s?|_uninstaller|setup|installer|package cache|temp|installshield"
                      r" installation information)(\\|$)", re.I)


def _infer_location(p):
    """Program folder when the registry does not say (from the icon or the uninstaller)."""
    prot = safety.protected_exact()
    win = norm_path(paths.kf("Windows"))
    icon = (p.get("icon") or "").rsplit(",", 1)[0] if re.search(r",\s*-?\d+$", p.get("icon") or "") else p.get("icon")
    for cand in (exe_from_command(icon), exe_from_command(p.get("uninstall"))):
        if not cand or not os.path.isabs(cand):
            continue
        d = norm_path(os.path.dirname(cand))
        if not d or d in prot or (win and paths.is_within(d, win)) or _BAD_LOC.search(d):
            continue
        # the VENDOR folder (AppData\Local\Xfer) is not the program's folder: it may hold other products
        base = paths.norm_name(os.path.basename(d))
        pub = paths.norm_name(re.sub(r"[,.]?\s*\b(inc|ltd|llc|gmbh|corp|corporation|records|software|"
                                     r"technologies|limited|co)\b\.?", "", p.get("publisher") or "", flags=re.I))
        pub_first = paths.norm_name((p.get("publisher") or "").split(" ")[0])
        if base and (base == pub or base == pub_first or (pub and pub.startswith(base) and len(base) >= 3)):
            continue
        if any(paths.is_within(d, f) for f in safety.forbidden_trees()) and not paths.is_within(
                d, os.path.join(paths.kf("LocalAppData") or "", "Microsoft", "WinGet", "Packages")):
            continue
        if os.path.isdir(d):
            return os.path.dirname(cand)
    return ""


def _read_entry(hive, view, keyname):
    root = registry.HIVES[hive]
    path = f"{UNINSTALL}\\{keyname}"
    try:
        k = registry.open_key(root, path, view)
    except OSError:
        return None
    with k:
        def g(n):
            try:
                return winreg.QueryValueEx(k, n)[0]
            except OSError:
                return None
        name = g("DisplayName")
        if not name or g("SystemComponent") == 1 or g("ParentKeyName") or \
                str(g("ReleaseType") or "") in ("Update", "Hotfix", "Security Update"):
            return None
        return {
            "id": f"{hive}|{view}|{keyname}", "source": "win32", "keyname": keyname,
            "key": RegRef(hive, path, view).to_str(),
            "name": str(name).strip(), "version": str(g("DisplayVersion") or "").strip(),
            "publisher": str(g("Publisher") or "").strip(),
            "location": str(g("InstallLocation") or "").strip().strip('"'),
            "uninstall": str(g("UninstallString") or ""), "quiet": str(g("QuietUninstallString") or ""),
            "icon": str(g("DisplayIcon") or "").strip().strip('"'), "user": hive == "HKCU",
            "windows_installer": g("WindowsInstaller") == 1,
            "size": int(g("EstimatedSize") or 0) * 1024 if str(g("EstimatedSize") or "0").isdigit() else 0,
            "date": _fmt_date(g("InstallDate"), _key_date(k)),
            "url": str(g("URLInfoAbout") or g("HelpLink") or ""),
            "no_remove": g("NoRemove") == 1,
            "arch": "32-bit" if view == V32 else ("64-bit" if hive == "HKLM" else "Current user"),
        }


def _finish(p):
    if re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", p["keyname"]) and (p["windows_installer"] or "msiexec" in
                                                                   p["uninstall"].lower()):
        p["msi_code"] = p["keyname"].upper()
    else:
        m = re.search(r"(\{[0-9A-Fa-f-]{36}\})", p["uninstall"]) if "msiexec" in p["uninstall"].lower() else None
        p["msi_code"] = m.group(1).upper() if m else ""
    p["kind"] = detect_kind(p)
    if not p["location"] and p["msi_code"]:
        p["location"] = (msi_info(p["msi_code"], "InstallLocation") or "").strip('"')
    p["location_inferred"] = False
    if not p["location"]:
        p["location"] = _infer_location(p)
        p["location_inferred"] = bool(p["location"])
    if p["location"] and norm_path(p["location"]) in safety.protected_exact():
        p["location"] = ""  # an InstallLocation of "C:\Program Files" is not the program's folder
    exe = exe_from_command(p["uninstall"])
    p["broken"] = bool(p["uninstall"]) and p["kind"] not in ("msi", "winget") and bool(exe) and \
        os.path.isabs(exe) and not os.path.exists(exe)
    p["orphan"] = bool(p["location"]) and not os.path.isdir(p["location"])
    p["no_uninstaller"] = not p["uninstall"] and not p["msi_code"]
    p["protected"] = safety.classify_program(p)
    p["kind_label"] = KIND_LABEL.get(p["kind"], p["kind"])
    p["can_quiet"] = can_quiet(p)
    return p


def get_win32_programs():
    progs, seen = [], set()
    for hive, view in SOURCES:
        for keyname in registry.subkeys(registry.HIVES[hive], UNINSTALL, view):
            p = _read_entry(hive, view, keyname)
            if not p:
                continue
            ident = (p["name"].lower(), p["version"].lower(), p["user"])
            if ident in seen:  # same entry seen through both registry views
                continue
            seen.add(ident)
            progs.append(_finish(p))
    return progs


# ---------------------------------------------------------------- Microsoft Store
def _indirect_string(s):
    if not s or not s.startswith("@"):
        return s
    buf = ctypes.create_unicode_buffer(1024)
    try:
        if ctypes.windll.shlwapi.SHLoadIndirectString(s, buf, 1024, None) == 0:
            return buf.value
    except Exception:
        pass
    return None


def _store_logo(location, logo_rel):
    if not logo_rel or not location:
        return ""
    base = os.path.join(location, logo_rel)
    d, stem = os.path.dirname(base), os.path.splitext(os.path.basename(base))[0]
    if os.path.isfile(base):
        return base
    try:
        cands = [f for f in os.listdir(d) if f.lower().startswith(stem.lower()) and f.lower().endswith(".png")]
    except OSError:
        return ""
    for pref in ("targetsize-48.", "targetsize-48_altform-unplated", "scale-200", "scale-100", ""):
        for f in cands:
            if pref in f.lower():
                return os.path.join(d, f)
    return ""


STORE_ESSENTIAL = {
    "Microsoft.WindowsStore", "Microsoft.DesktopAppInstaller", "Microsoft.StorePurchaseApp",
    "Microsoft.SecHealthUI", "Microsoft.WindowsAppRuntime", "Microsoft.Winget.Source",
    "Microsoft.Services.Store.Engagement", "Microsoft.WidgetsPlatformRuntime", "Microsoft.WebMediaExtensions",
    "Microsoft.HEIFImageExtension", "Microsoft.VP9VideoExtensions", "Microsoft.WebpImageExtension",
    "Microsoft.HEVCVideoExtension", "Microsoft.AV1VideoExtension", "Microsoft.RawImageExtension",
    "Microsoft.MPEG2VideoExtension", "MicrosoftWindows.Client.WebExperience", "Microsoft.ApplicationCompatibilityEnhancements",
}


def _resolve_resource(s, a):
    """Turn 'ms-resource:AppName' into the real text; return None if that is not possible."""
    s = (s or "").strip()
    if not s:
        return None
    if not s.lower().startswith("ms-resource:"):
        return s
    key = s.split(":", 1)[1]
    full, name = a["Full"], a["Name"]
    cands = [f"ms-resource:{key}"] if key.startswith("//") else [
        f"ms-resource://{name}/resources/{key}", f"ms-resource://{name}/{key}", f"ms-resource:///resources/{key}"]
    for c in cands:
        v = _indirect_string(f"@{{{full}?{c}}}")
        if v and not v.lower().startswith("ms-resource"):
            return v
    return None


def _pretty_package_name(n):
    """'Microsoft.WindowsNotepad' -> 'Windows Notepad'."""
    last = n.split(".")[-1] if "." in n else n
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", last)


def _manifest_props_ps(full):
    full = (full or "").replace("'", "")
    if not full:
        return {}
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          f"$m = Get-AppxPackageManifest -Package '{full}'; [pscustomobject]@{{ "
          "DisplayName=[string]$m.Package.Properties.DisplayName; "
          "PublisherDisplayName=[string]$m.Package.Properties.PublisherDisplayName; "
          "Logo=[string]$m.Package.Properties.Logo } | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NOWIN, timeout=30)
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return {}


def _manifest_props(loc):
    """DisplayName, PublisherDisplayName and Logo read from AppxManifest.xml (or None if not possible)."""
    try:
        with open(os.path.join(loc, "AppxManifest.xml"), "rb") as f:
            root = ET.fromstring(f.read())
    except (OSError, ET.ParseError, TypeError):
        return None
    for el in root:
        if el.tag.rsplit("}", 1)[-1] == "Properties":
            return {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in el}
    return {}


def get_store_apps():
    """Store apps. Manifests are read from disk: Get-AppxPackageManifest per package
    took 1.8 s of the query's 2.2 s."""
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          "Get-AppxPackage | Where-Object { -not $_.IsFramework -and -not $_.NonRemovable -and "
          "$_.SignatureKind -ne 'System' -and -not $_.IsResourcePackage } | ForEach-Object { "
          "[pscustomobject]@{ Name=$_.Name; Full=$_.PackageFullName; Family=$_.PackageFamilyName; "
          "Ver=[string]$_.Version; Loc=$_.InstallLocation; Pub=$_.PublisherId; Sig=[string]$_.SignatureKind "
          "} } | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NOWIN, timeout=90)
        data = json.loads(r.stdout) if r.stdout.strip() else []
    except Exception:
        return []
    data = data if isinstance(data, list) else [data]
    for a in data:
        pr = _manifest_props(a.get("Loc"))
        if pr is None:  # unreadable manifest: ask Windows for this package only
            pr = _manifest_props_ps(a.get("Full", ""))
        a["Disp"], a["PubDisp"], a["Logo"] = (pr.get("DisplayName", ""), pr.get("PublisherDisplayName", ""),
                                              pr.get("Logo", ""))
    out = []
    for a in data:
        name = _resolve_resource(a.get("Disp"), a) or _pretty_package_name(a["Name"])
        pub = _resolve_resource(a.get("PubDisp"), a) or ""
        p = {
            "id": f"store|{a['Full']}", "source": "store", "keyname": a["Full"], "key": "",
            "name": name, "version": a.get("Ver", ""), "publisher": pub, "location": a.get("Loc") or "",
            "uninstall": "", "quiet": "", "icon": _store_logo(a.get("Loc"), a.get("Logo")), "user": True,
            "windows_installer": False, "size": 0, "date": "", "url": "", "no_remove": False,
            "arch": "", "kind": "store", "kind_label": KIND_LABEL["store"], "msi_code": "",
            "package": a["Full"], "family": a.get("Family", ""), "package_name": a["Name"],
            "broken": False, "orphan": False, "no_uninstaller": False, "location_inferred": False,
            "can_quiet": True, "signature": a.get("Sig", ""),
        }
        try:
            p["date"] = datetime.datetime.fromtimestamp(os.path.getctime(p["location"])).strftime("%Y-%m-%d")
        except OSError:
            pass
        if a["Name"] in STORE_ESSENTIAL:
            p["protected"] = {"category": "windows", "label": "Essential part of Windows", "no_force": True,
                              "scan_level_max": "safe", "blocked": True}
        else:
            p["protected"] = safety.classify_program(p)
            if p["protected"] and p["protected"]["category"] in ("microsoft", "windows"):
                p["protected"] = None  # Microsoft's Store apps can be removed safely
        out.append(p)
    return out


_STORE_CACHE = "store.json"


def cached_store_apps():
    """Last Store list saved on disk (instant). Only for DISPLAY: it is refreshed right away."""
    try:
        with open(paths.data_path("cache", _STORE_CACHE), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        for p in data:  # caches saved by the Spanish build said "Tienda"
            if isinstance(p, dict) and p.get("arch") == "Tienda":
                p["arch"] = ""
        return data
    except (OSError, ValueError):
        return []


def save_store_cache(apps):
    try:
        tmp = paths.data_path("cache", _STORE_CACHE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(apps, f, ensure_ascii=False)
        os.replace(tmp, paths.data_path("cache", _STORE_CACHE))
    except OSError:
        pass


def get_programs(include_store=True):
    progs = get_win32_programs()
    if include_store:
        progs += get_store_apps()
    progs.sort(key=lambda p: p["name"].lower())
    return progs


# ---------------------------------------------------------------- sizes
_SIZE_CACHE = None


def _size_cache():
    global _SIZE_CACHE
    if _SIZE_CACHE is None:
        try:
            with open(paths.data_path("cache", "sizes.json"), encoding="utf-8") as f:
                _SIZE_CACHE = json.load(f)
        except Exception:
            _SIZE_CACHE = {}
    return _SIZE_CACHE


def save_size_cache():
    if _SIZE_CACHE is not None:
        try:
            with open(paths.data_path("cache", "sizes.json"), "w", encoding="utf-8") as f:
                json.dump(_SIZE_CACHE, f)
        except OSError:
            pass


def real_size(p, cached_only=False):
    """Real size of the program folder (cached by modification date).

    cached_only: return the cached value if it is still valid, and None instead of measuring.
    """
    loc = p.get("location")
    if not loc or not os.path.isdir(loc) or p.get("location_inferred") and p.get("source") != "win32":
        return None
    if norm_path(loc) in safety.protected_exact():
        return None
    try:
        mt = os.path.getmtime(loc)
    except OSError:
        return None
    c = _size_cache()
    key = norm_path(loc)
    hit = c.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    if cached_only:
        return None
    size = paths.folder_size(loc)[0]
    c[key] = [mt, size]
    return size

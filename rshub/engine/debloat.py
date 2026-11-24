"""Super Optimizer · Debloat: preinstalled apps, ads and UI clutter. Safe and reversible.

- Apps: only packages in a closed catalog are offered. Removal is for the current user (Remove-AppxPackage);
  optionally also for new users (deprovision, administrator). Every removal is recorded with what is needed to
  bring it back (register the files that stay on disk, or install it again from the Microsoft Store); when
  neither is possible the UI says so before and after.
- Tweaks: a closed list of registry values. Before changing one, its previous state is saved (value, type, or
  "did not exist", and which keys were created) so Undo puts back exactly what was there. Without a backup,
  Undo writes the Windows default.
- Privacy toggles (telemetry, advertising ID, activity history…) belong to the Privacy & Security module and
  are not duplicated here.

The registry code goes through a small backend (`WinReg`) so it can be tested with a fake one.
"""
import json
import os
import re
import sys
import threading
import time
import winreg
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import shutil

from ..i18n import tr
from .optimizer import USER_DATA, user_needs_admin, write_json_atomic
from .ps import ps, ps_json

EXPORTS = {"tweaks_list", "tweak_undo", "history", "open_store"}
JOBS = {"apps_scan", "apply", "reinstall", "undo_all"}

# What RsHub changed for THIS user (HKCU values, removed apps) lives in the user's own profile: a folder in
# ProgramData created by an elevated run is read-only for the same user's later non-elevated runs, so the records
# could not be saved. Other users can't read it either.
STATE_DIR = os.path.join(USER_DATA, "debloat")
_LEGACY_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "RsHub", "optimizer", "debloat")
_DEFAULT_STATE_DIR = STATE_DIR
_state_lock = threading.Lock()
_migrated = set()


def _migrate_legacy():
    """Once per run: copies this user's records from the old ProgramData folder to STATE_DIR (never overwrites)."""
    if STATE_DIR != _DEFAULT_STATE_DIR or STATE_DIR in _migrated:
        return
    _migrated.add(STATE_DIR)
    user = re.sub(r"[^A-Za-z0-9._-]", "_", os.environ.get("USERNAME") or "user")
    for name in (f"state-{user}.json", "sizes.json"):
        src, dst = os.path.join(_LEGACY_DIR, name), os.path.join(STATE_DIR, name)
        try:
            if os.path.isfile(src) and not os.path.exists(dst):
                os.makedirs(STATE_DIR, exist_ok=True)
                shutil.copy2(src, dst)
        except OSError:
            pass


def _admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _build():
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


# =============================================================== apps: the catalog
# group: safe (promotions and apps few people use, easy to get back), choice (useful to some people),
# keep (Windows or other apps need it: shown, never selectable).
# store: Microsoft Store product id (winget --source msstore) when known; the Store page by family name is the
# fallback. gone: Microsoft no longer offers it, so once its files are gone it cannot be installed again.
CATALOG = [
    # ---- safe to remove
    {"m": "king.com.*", "g": "safe", "n": "King games (Candy Crush and others)", "d": "Games Windows installs as a promotion."},
    {"m": "Microsoft.BingNews", "g": "safe", "n": "Microsoft News", "d": "News feed with ads. The same news is on the web.", "store": "9WZDNCRFHVFW"},
    {"m": "Microsoft.BingWeather", "g": "safe", "n": "Weather", "d": "MSN Weather. The forecast is also in Widgets and on the web.", "store": "9WZDNCRFJ3Q2"},
    {"m": "Microsoft.BingSearch", "g": "safe", "n": "Bing Search", "d": "Bing app that puts web results in Windows search."},
    {"m": "Microsoft.BingFinance", "g": "safe", "n": "Money", "d": "Old MSN Money app.", "gone": True},
    {"m": "Microsoft.BingSports", "g": "safe", "n": "Sports", "d": "Old MSN Sports app.", "gone": True},
    {"m": "Microsoft.BingTranslator", "g": "safe", "n": "Translator", "d": "Old Microsoft Translator app."},
    {"m": "Microsoft.MicrosoftSolitaireCollection", "g": "safe", "n": "Solitaire Collection", "d": "Card games with ads and a paid subscription.", "store": "9WZDNCRFHWD2"},
    {"m": "Microsoft.GetHelp", "g": "safe", "n": "Get Help", "d": "Opens Microsoft support articles. Settings already links to them.", "store": "9PKDZBMV1H3T"},
    {"m": "Microsoft.Getstarted", "g": "safe", "n": "Tips", "d": "Tips about Windows features."},
    {"m": "Microsoft.WindowsFeedbackHub", "g": "safe", "n": "Feedback Hub", "d": "Sends suggestions and bug reports to Microsoft.", "store": "9NBLGGH4R32N"},
    {"m": "Microsoft.MicrosoftOfficeHub", "g": "safe", "n": "Microsoft 365 Copilot", "d": "Launcher that advertises Microsoft 365. Word, Excel and the rest keep working without it.", "store": "9WZDNCRD29V9"},
    {"m": "Clipchamp.Clipchamp", "g": "safe", "n": "Clipchamp", "d": "Online video editor. Many of its features need an account.", "store": "9P1J8S7CCWWT"},
    {"m": "MicrosoftTeams", "g": "safe", "n": "Teams (personal)", "d": "The chat app from the first Windows 11 versions, already retired.", "gone": True},
    {"m": "Microsoft.SkypeApp", "g": "safe", "n": "Skype", "d": "Microsoft retired Skype in 2025.", "gone": True},
    {"m": "Microsoft.People", "g": "safe", "n": "People", "d": "Old contacts app.", "gone": True},
    {"m": "Microsoft.Messaging", "g": "safe", "n": "Messaging", "d": "Old messaging app.", "gone": True},
    {"m": "Microsoft.OneConnect", "g": "safe", "n": "Mobile Plans", "d": "Buys mobile data plans; almost no PC uses it.", "gone": True},
    {"m": "Microsoft.Wallet", "g": "safe", "n": "Wallet", "d": "Old payments app.", "gone": True},
    {"m": "Microsoft.MixedReality.Portal", "g": "safe", "n": "Mixed Reality Portal", "d": "Only for Windows Mixed Reality headsets, which Microsoft discontinued.", "gone": True},
    {"m": "Microsoft.Microsoft3DViewer", "g": "safe", "n": "3D Viewer", "d": "Viewer for 3D models.", "gone": True},
    {"m": "Microsoft.Print3D", "g": "safe", "n": "Print 3D", "d": "Old 3D printing app.", "gone": True},
    {"m": "Microsoft.MSPaint", "g": "safe", "n": "Paint 3D", "d": "Microsoft retired Paint 3D. The classic Paint is a different app.", "gone": True},
    {"m": "Microsoft.549981C3F5F10", "g": "safe", "n": "Cortana", "d": "Microsoft retired Cortana.", "gone": True},
    {"m": "Microsoft.ZuneVideo", "g": "safe", "n": "Films & TV", "d": "Old video player and store. Media Player plays videos too.", "store": "9WZDNCRFJ3P2"},
    {"m": "Microsoft.WindowsMaps", "g": "safe", "n": "Maps", "d": "Offline maps app that Microsoft is retiring.", "gone": True},
    {"m": "Microsoft.PowerAutomateDesktop", "g": "safe", "n": "Power Automate", "d": "Automation tool for businesses.", "store": "9NFTCH6J7FHV"},
    {"m": "Microsoft.Windows.DevHome", "g": "safe", "n": "Dev Home", "d": "Developer dashboard that Microsoft discontinued.", "gone": True},
    {"m": "Microsoft.MicrosoftJournal", "g": "safe", "n": "Journal", "d": "Handwritten notes for pens."},
    {"m": "Microsoft.BingHealthAndFitness", "g": "safe", "n": "Health & Fitness", "d": "Old MSN app.", "gone": True},
    {"m": "Microsoft.BingTravel", "g": "safe", "n": "Travel", "d": "Old MSN app.", "gone": True},
    {"m": "Microsoft.BingFoodAndDrink", "g": "safe", "n": "Food & Drink", "d": "Old MSN app.", "gone": True},
    # ---- your choice
    {"m": "Microsoft.Copilot", "g": "choice", "n": "Copilot", "d": "Microsoft's AI assistant. Remove it if you don't use it.", "store": "9NHT9RB2F4HD"},
    {"m": "MSTeams", "g": "choice", "n": "Microsoft Teams", "d": "Chat and video calls. Keep it if you use it for work or school.", "store": "XP8BT8DW290MPQ"},
    {"m": "Microsoft.OutlookForWindows", "g": "choice", "n": "Outlook (new)", "d": "The new Outlook for email and calendar.", "store": "9NRX63209R7B"},
    {"m": "microsoft.windowscommunicationsapps", "g": "choice", "n": "Mail and Calendar", "d": "Old email app; Microsoft replaced it with the new Outlook.", "gone": True},
    {"m": "Microsoft.Office.OneNote", "g": "choice", "n": "OneNote for Windows 10", "d": "Old version of OneNote.", "store": "9WZDNCRFHVJL"},
    {"m": "Microsoft.Todos", "g": "choice", "n": "Microsoft To Do", "d": "Task lists synced with your Microsoft account.", "store": "9NBLGGH5R558"},
    {"m": "Microsoft.MicrosoftStickyNotes", "g": "choice", "n": "Sticky Notes", "d": "Notes on the desktop.", "store": "9NBLGGH4QGHW"},
    {"m": "Microsoft.GamingApp", "g": "choice", "n": "Xbox", "d": "Game Pass and the Xbox store. Keep it if you play PC Game Pass.", "store": "9MV0B5HZVK9Z"},
    {"m": "Microsoft.XboxApp", "g": "choice", "n": "Xbox Console Companion", "d": "Old Xbox app.", "gone": True},
    {"m": "Microsoft.XboxGamingOverlay", "g": "choice", "n": "Game Bar", "d": "Win+G: captures, performance and chat in games. Some games rely on it to record clips.", "store": "9NZKPSTSNW4P"},
    {"m": "Microsoft.XboxSpeechToTextOverlay", "g": "choice", "n": "Xbox speech to text", "d": "Accessibility in Xbox party chat."},
    {"m": "Microsoft.YourPhone", "g": "choice", "n": "Phone Link", "d": "Your phone's messages, calls and photos on the PC.", "store": "9NMPJ99VJBWV"},
    {"m": "Microsoft.ZuneMusic", "g": "choice", "n": "Media Player", "d": "The default Windows player for music and video. Without it, files open in another player you have.", "store": "9WZDNCRFJ3PT"},
    {"m": "Microsoft.WindowsAlarms", "g": "choice", "n": "Clock", "d": "Alarms, timers and Focus sessions.", "store": "9WZDNCRFJ3PR"},
    {"m": "Microsoft.WindowsSoundRecorder", "g": "choice", "n": "Sound Recorder", "d": "Records audio from the microphone.", "store": "9WZDNCRFHWKN"},
    {"m": "Microsoft.WindowsCamera", "g": "choice", "n": "Camera", "d": "Takes photos and videos with the webcam. Video call apps don't need it.", "store": "9WZDNCRFJBBG"},
    {"m": "Microsoft.Windows.Photos", "g": "choice", "n": "Photos", "d": "The default image viewer. Without it, images open in another viewer you have.", "store": "9WZDNCRFJBH4"},
    {"m": "Microsoft.Paint", "g": "choice", "n": "Paint", "d": "Simple image editor.", "store": "9PCFS5B6T72H"},
    {"m": "Microsoft.ScreenSketch", "g": "choice", "n": "Snipping Tool", "d": "Screenshots with Win+Shift+S and the Print Screen key.", "store": "9MZ95KL8MR0L"},
    {"m": "Microsoft.WindowsCalculator", "g": "choice", "n": "Calculator", "d": "Almost everyone uses it.", "store": "9WZDNCRFHVN5"},
    {"m": "Microsoft.WindowsNotepad", "g": "choice", "n": "Notepad", "d": "Plain text editor that many programs open.", "store": "9MSMLRH6LZF3"},
    {"m": "MicrosoftCorporationII.QuickAssist", "g": "choice", "n": "Quick Assist", "d": "Lets someone help you remotely. Scammers often ask people to open it.", "store": "9P7BP5VNWKX5"},
    {"m": "MicrosoftWindows.CrossDevice", "g": "choice", "n": "Mobile devices", "d": "Uses your phone as a webcam or shows its notifications on the PC."},
    {"m": "SpotifyAB.SpotifyMusic", "g": "choice", "n": "Spotify", "d": "Music. Often preinstalled as a promotion.", "store": "9NCBCSZSJRSB"},
    {"m": "Disney.37853FC22B2CE", "g": "choice", "n": "Disney+", "d": "Usually preinstalled as a promotion.", "store": "9NXQXXLFST89"},
    {"m": "AmazonVideo.PrimeVideo", "g": "choice", "n": "Prime Video", "d": "Usually preinstalled as a promotion.", "store": "9P6RC76MSMMJ"},
    {"m": "4DF9E0F8.Netflix", "g": "choice", "n": "Netflix", "d": "Usually preinstalled as a promotion.", "store": "9WZDNCRFJ3TJ"},
    {"m": "BytedancePte.Ltd.TikTok", "g": "choice", "n": "TikTok", "d": "Usually preinstalled as a promotion."},
    {"m": "Facebook.Facebook", "g": "choice", "n": "Facebook", "d": "Usually preinstalled as a promotion."},
    {"m": "Facebook.InstagramBeta", "g": "choice", "n": "Instagram", "d": "Usually preinstalled as a promotion."},
    {"m": "7EE7776C.LinkedInforWindows", "g": "choice", "n": "LinkedIn", "d": "Usually preinstalled as a promotion.", "store": "9WZDNCRFJ4Q7"},
    {"m": "5319275A.WhatsAppDesktop", "g": "choice", "n": "WhatsApp", "d": "Keep it if you use WhatsApp on the PC.", "store": "9NKSQGP7F2NH"},
    # ---- keep (Windows or other apps need it)
    {"m": "Microsoft.WindowsStore", "g": "keep", "n": "Microsoft Store", "d": "Without it you can't install or update Store apps."},
    {"m": "Microsoft.StorePurchaseApp", "g": "keep", "n": "Store purchases", "d": "The Microsoft Store needs it to buy and download."},
    {"m": "Microsoft.DesktopAppInstaller", "g": "keep", "n": "App Installer (winget)", "d": "Installs and updates programs; RsHub uses it too."},
    {"m": "Microsoft.Winget.Source", "g": "keep", "n": "winget catalog", "d": "The list of programs winget can install."},
    {"m": "Microsoft.SecHealthUI", "g": "keep", "n": "Windows Security", "d": "The antivirus and firewall settings."},
    {"m": "Microsoft.WindowsTerminal", "g": "keep", "n": "Terminal", "d": "The Windows 11 console: command prompts open in it."},
    {"m": "Microsoft.XboxIdentityProvider", "g": "keep", "n": "Xbox Identity Provider", "d": "Games use it to sign in to your Microsoft account."},
    {"m": "Microsoft.Xbox.TCUI", "g": "keep", "n": "Xbox TCUI", "d": "Sign-in and friends windows inside games."},
    {"m": "MicrosoftWindows.Client.WebExperience", "g": "keep", "n": "Widgets board", "d": "To hide Widgets, use the Widgets setting below instead."},
    {"m": "Microsoft.WidgetsPlatformRuntime", "g": "keep", "n": "Widgets platform", "d": "Other apps' widgets depend on it."},
    {"m": "Microsoft.ApplicationCompatibilityEnhancements", "g": "keep", "n": "App compatibility", "d": "Helps older apps and some codecs work."},
    {"m": "Microsoft.MicrosoftEdge.Stable", "g": "keep", "n": "Microsoft Edge", "d": "Windows and many apps open web content with it."},
    {"m": "Microsoft.*Extension*", "g": "keep", "n": "Media extensions", "d": "Codecs so Photos and other apps open HEIF, HEVC, WebP, AV1 and RAW files."},
    {"m": "Microsoft.LanguageExperiencePack*", "g": "keep", "n": "Language pack", "d": "The Windows display language."},
]


def _match(pattern, name):
    if "*" not in pattern:
        return pattern.lower() == name.lower()
    rx = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$"
    return re.match(rx, name, re.I) is not None


def catalog_entry(name):
    """The catalog entry for a package name, or None (exact names win over patterns)."""
    for c in CATALOG:
        if "*" not in c["m"] and _match(c["m"], name):
            return c
    for c in CATALOG:
        if "*" in c["m"] and _match(c["m"], name):
            return c
    return None


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-~+ ]+$")


def _q(s):
    """PowerShell single-quoted literal; only package-like names get through."""
    s = str(s or "")
    if not _SAFE_NAME.match(s):
        raise ValueError(f"unexpected package name: {s!r}")
    return "'" + s.replace("'", "''") + "'"


def remove_script(full_name, name, deprovision):
    """PowerShell that removes a package for the current user and, optionally, for new users."""
    s = f"Remove-AppxPackage -Package {_q(full_name)} -ErrorAction Stop; 'removed'"
    if deprovision:
        s += (f"; Get-AppxProvisionedPackage -Online | Where-Object DisplayName -eq {_q(name)} | "
              "Remove-AppxProvisionedPackage -Online -ErrorAction Stop | Out-Null; 'deprovisioned'")
    return s


def register_script(manifest):
    if not re.fullmatch(r"[A-Za-z]:\\[^'\"\r\n]+\\AppxManifest\.xml", manifest or "", re.I):
        raise ValueError(manifest)
    return f"Add-AppxPackage -Register '{manifest}' -DisableDevelopmentMode -ErrorAction Stop; 'ok'"


def winget_args(store_id):
    if not re.fullmatch(r"[A-Z0-9]{12,14}", store_id or ""):
        raise ValueError(store_id)
    return ["winget", "install", "--id", store_id, "--source", "msstore", "--accept-package-agreements",
            "--accept-source-agreements", "--silent", "--disable-interactivity"]


def restore_plan(rec, manifest_exists):
    """How a removed app can come back: 'register' (its files are still on disk), 'store' (install it again from
    the Microsoft Store) or 'none'. rec: what was recorded when it was removed."""
    if manifest_exists:
        return "register"
    if rec.get("gone"):
        return "none"
    if rec.get("store") or rec.get("family"):
        return "store"
    return "none"


# =============================================================== apps: reading what is installed
def _logo(loc):
    """Small logo of a Store app (Square44x44Logo, like the Start menu), as a PNG path."""
    try:
        with open(os.path.join(loc, "AppxManifest.xml"), "rb") as f:
            root = ET.fromstring(f.read())
    except (OSError, ET.ParseError, TypeError):
        return ""
    rel = ""
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "VisualElements" and el.get("Square44x44Logo"):
            rel = el.get("Square44x44Logo")
            break
        if tag == "Logo" and el.text and not rel:
            rel = el.text.strip()
    if not rel:
        return ""
    from .removal.programs import _store_logo
    return _store_logo(loc, rel)


def _icon(loc):
    p = _logo(loc)
    if not p:
        return None
    try:
        from .removal import icons
        return icons.get_icon({"source": "store", "icon": p})
    except Exception:
        return None


def _folder_size(path):
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _size_cache_path():
    return os.path.join(STATE_DIR, "sizes.json")


def _load_json(p, default):
    _migrate_legacy()
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(p, data):
    """Raises OSError when the file can't be written (callers that hold an undo record must not carry on)."""
    write_json_atomic(p, data)


def _installed():
    rows = ps_json("Get-AppxPackage | Where-Object { -not $_.IsFramework -and -not $_.IsResourcePackage -and -not $_.NonRemovable } | "
                   "Select-Object Name, PackageFullName, PackageFamilyName, InstallLocation, @{n='Version';e={[string]$_.Version}}",
                   timeout=60, as_list=True)
    return [r for r in rows if r.get("Name")]


def _provisioned():
    """Names of the packages Windows installs for every new user (administrator only; None if unknown)."""
    if not _admin():
        return None
    rows = ps_json("Get-AppxProvisionedPackage -Online | Select-Object DisplayName", timeout=60, as_list=True)
    return {r.get("DisplayName") for r in rows if r.get("DisplayName")}


def _item(r, prov):
    c = catalog_entry(r["Name"])
    if not c:
        return None
    return {"id": r["PackageFullName"], "name": r["Name"], "family": r.get("PackageFamilyName") or "",
            "label": tr(c["n"]), "desc": tr(c["d"]), "g": c["g"], "loc": r.get("InstallLocation") or "",
            "version": r.get("Version") or "", "store": c.get("store") or "", "gone": bool(c.get("gone")),
            "prov": (r["Name"] in prov) if prov is not None else None, "size": None, "icon": None,
            "restore": "none" if c.get("gone") else "store"}


def apps_scan(job):
    """The catalogued apps that are installed, with icon and size. Changes nothing."""
    job.progress(.05, tr("Reading the installed apps…"))
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_inst, f_prov = ex.submit(_installed), ex.submit(_provisioned)
        inst, prov = f_inst.result(), f_prov.result()
    items = [it for it in (_item(r, prov) for r in inst) if it]
    # the same app can appear once per version: keep the newest
    seen = {}
    for it in items:
        k = it["name"].lower()
        if k not in seen or it["version"] > seen[k]["version"]:
            seen[k] = it
    items = sorted(seen.values(), key=lambda x: ({"safe": 0, "choice": 1, "keep": 2}[x["g"]], x["label"].lower()))
    job.progress(.35, tr("Reading the installed apps…"), items=items, force=True)
    sizes = _load_json(_size_cache_path(), {})

    def one(it):
        if it["loc"]:
            it["icon"] = _icon(it["loc"])
            if it["id"] in sizes:
                it["size"] = sizes[it["id"]]
            elif os.path.isdir(it["loc"]):
                it["size"] = _folder_size(it["loc"])
                sizes[it["id"]] = it["size"]
        return it
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for _ in ex.map(one, items):
            done += 1
            job.progress(.35 + .6 * done / max(1, len(items)), tr("Measuring the apps…"))
    try:
        _save_json(_size_cache_path(), {k: v for k, v in sizes.items() if any(k == i["id"] for i in items)})
    except OSError:
        pass  # only a cache of folder sizes
    # “keep” entries that are several packages (media extensions, language packs) are shown as one row
    merged, out = {}, []
    for it in items:
        if it["g"] != "keep":
            out.append(it)
            continue
        m = merged.get(it["label"])
        if m is None:
            merged[it["label"]] = m = dict(it, count=1)
            out.append(m)
        else:
            m["count"] += 1
            m["size"] = (m["size"] or 0) + (it["size"] or 0)
    items = out
    job.progress(1, tr("Done"))
    return {"items": items, "admin": _admin(), "provKnown": prov is not None, "removed": history()["apps"]}


# =============================================================== tweaks: definitions
HKCU, HKLM = "HKCU", "HKLM"
DW, SZ = "dword", "sz"
_ADV = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_CDM = r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"


def V(hive, key, name, value, default=None, typ=DW):
    """A registry value: what debloating writes (value) and what Windows has by default (default; None = the
    value does not exist)."""
    return {"hive": hive, "key": key, "name": name, "type": typ, "value": value, "default": default}


# g: ads, ai, taskbar, explorer. rec: part of the Recommended preset. explorer: File Explorer must restart to
# show it. when: only offered if present on this PC.
TWEAKS = [
    {"id": "start_recs", "g": "ads", "rec": True, "t": "Suggestions and ads in Start",
     "d": "Promoted apps and “recommendations” for tips, shortcuts and new apps in the Start menu.",
     "v": [V(HKCU, _CDM, "SystemPaneSuggestionsEnabled", 0), V(HKCU, _CDM, "SubscribedContent-338388Enabled", 0),
           V(HKCU, _ADV, "Start_IrisRecommendations", 0)]},
    {"id": "auto_install", "g": "ads", "rec": True, "t": "Apps Windows installs by itself",
     "d": "Stops Windows from silently installing promoted apps and games such as Candy Crush.",
     "v": [V(HKCU, _CDM, "SilentInstalledAppsEnabled", 0), V(HKCU, _CDM, "OemPreInstalledAppsEnabled", 0),
           V(HKCU, _CDM, "PreInstalledAppsEnabled", 0)]},
    {"id": "lock_ads", "g": "ads", "rec": True, "t": "Ads and fun facts on the lock screen",
     "d": "The tips and promotions shown over the lock screen picture. The picture itself stays.",
     "v": [V(HKCU, _CDM, "RotatingLockScreenOverlayEnabled", 0), V(HKCU, _CDM, "SubscribedContent-338387Enabled", 0)]},
    {"id": "settings_ads", "g": "ads", "rec": True, "t": "Suggestions in Settings",
     "d": "Suggested content and offers at the top of the Settings app.",
     "v": [V(HKCU, _CDM, "SubscribedContent-338393Enabled", 0), V(HKCU, _CDM, "SubscribedContent-353694Enabled", 0),
           V(HKCU, _CDM, "SubscribedContent-353696Enabled", 0)]},
    {"id": "tips", "g": "ads", "rec": True, "t": "Tips and tricks notifications",
     "d": "Notifications with tips and suggestions while you use Windows.",
     "v": [V(HKCU, _CDM, "SubscribedContent-338389Enabled", 0), V(HKCU, _CDM, "SoftLandingEnabled", 0)]},
    {"id": "welcome", "g": "ads", "rec": True, "t": "“Get even more out of Windows”",
     "d": "The full-screen setup prompts after updates that push Microsoft 365, OneDrive and Game Pass.",
     "v": [V(HKCU, r"Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement", "ScoobeSystemSettingEnabled", 0),
           V(HKCU, _CDM, "SubscribedContent-310093Enabled", 0)]},
    {"id": "account_ads", "g": "ads", "rec": True, "t": "Account notifications in Start",
     "d": "Badges on your picture in Start that suggest backing up or buying Microsoft 365.",
     "v": [V(HKCU, _ADV, "Start_AccountNotifications", 0)]},
    {"id": "explorer_ads", "g": "ads", "rec": True, "explorer": True, "t": "Ads in File Explorer",
     "d": "Notices from sync providers (such as OneDrive offers) at the top of File Explorer.",
     "v": [V(HKCU, _ADV, "ShowSyncProviderNotifications", 0)]},
    {"id": "edge_shortcut", "g": "ads", "rec": True, "t": "Edge shortcut on the desktop after updates",
     "d": "Edge puts its shortcut back on the desktop every time it updates. This stops it.",
     "v": [V(HKLM, r"SOFTWARE\Policies\Microsoft\EdgeUpdate", "CreateDesktopShortcutDefault", 0)]},
    {"id": "web_search", "g": "ai", "rec": True, "t": "Bing and web results in Start search",
     "d": "Searching from Start only shows apps, files and settings on your PC.",
     "v": [V(HKCU, r"Software\Policies\Microsoft\Windows\Explorer", "DisableSearchBoxSuggestions", 1),
           V(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Search", "BingSearchEnabled", 0)]},
    {"id": "search_highlights", "g": "ai", "rec": True, "t": "Search highlights",
     "d": "The daily illustrations and trending searches inside the search box.",
     "v": [V(HKCU, r"Software\Microsoft\Windows\CurrentVersion\SearchSettings", "IsDynamicSearchBoxEnabled", 0)]},
    {"id": "widgets", "g": "ai", "rec": True, "explorer": True, "t": "Widgets",
     "d": "Turns off the Widgets board with news and weather on the taskbar.",
     "v": [V(HKLM, r"SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests", 0)]},
    {"id": "copilot", "g": "ai", "rec": True, "explorer": True, "t": "Copilot in Windows",
     "d": "Hides the Copilot button and turns off Copilot built into Windows. The Copilot app, if you have it, is in the apps list.",
     "v": [V(HKCU, r"Software\Policies\Microsoft\Windows\WindowsCopilot", "TurnOffWindowsCopilot", 1),
           V(HKCU, _ADV, "ShowCopilotButton", 0)]},
    {"id": "recall", "g": "ai", "rec": True, "when": "recall", "t": "Recall",
     "d": "Stops Windows from saving snapshots of your screen to search them later.",
     "v": [V(HKCU, r"Software\Policies\Microsoft\Windows\WindowsAI", "DisableAIDataAnalysis", 1)]},
    {"id": "tb_left", "g": "taskbar", "explorer": True, "t": "Align the taskbar to the left",
     "d": "Start and the apps on the left, like in Windows 10.",
     "v": [V(HKCU, _ADV, "TaskbarAl", 0)]},
    {"id": "tb_search", "g": "taskbar", "explorer": True, "t": "Hide the search box",
     "d": "Start still searches when you type after opening it.",
     "v": [V(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Search", "SearchboxTaskbarMode", 0)]},
    {"id": "tb_taskview", "g": "taskbar", "explorer": True, "t": "Hide Task view",
     "d": "Win+Tab still opens it.",
     "v": [V(HKCU, _ADV, "ShowTaskViewButton", 0)]},
    {"id": "tb_chat", "g": "taskbar", "rec": True, "explorer": True, "when": "chat", "t": "Hide Chat",
     "d": "The Teams chat button from the first Windows 11 versions.",
     "v": [V(HKCU, _ADV, "TaskbarMn", 0)]},
    {"id": "classic_menu", "g": "explorer", "explorer": True, "t": "Classic right-click menu",
     "d": "The full right-click menu without “Show more options”.",
     "v": [V(HKCU, r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32", "", "", typ=SZ)]},
    {"id": "extensions", "g": "explorer", "rec": True, "explorer": True, "t": "Show file extensions",
     "d": "Shows .exe, .pdf and the rest, so a “photo.jpg.exe” can't fool you.",
     "v": [V(HKCU, _ADV, "HideFileExt", 0, default=1)]},
    {"id": "hidden", "g": "explorer", "explorer": True, "t": "Show hidden files",
     "d": "Shows hidden files and folders such as AppData. System files stay hidden.",
     "v": [V(HKCU, _ADV, "Hidden", 1, default=2)]},
    {"id": "this_pc", "g": "explorer", "explorer": True, "t": "Open File Explorer in This PC",
     "d": "Opens on your drives instead of Home with recent files.",
     "v": [V(HKCU, _ADV, "LaunchTo", 1)]},
]
GROUPS = [("ads", "Ads and suggestions"), ("ai", "Search, Widgets and AI"), ("taskbar", "Taskbar"),
          ("explorer", "File Explorer")]


def present(tw, build=None, recall=None):
    """Whether a tweak makes sense on this PC."""
    w = tw.get("when")
    if not w:
        return True
    build = _build() if build is None else build
    if w == "chat":
        return 22000 <= build < 22631
    if w == "recall":
        if recall is None:
            recall = os.path.isdir(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "SystemApps",
                                                "MicrosoftWindows.Client.AIX_cw5n1h2txyewy"))
        return build >= 26100 and recall
    return True


# =============================================================== tweaks: registry backend
_ROOT = {HKCU: winreg.HKEY_CURRENT_USER, HKLM: winreg.HKEY_LOCAL_MACHINE}
_TYPE = {DW: winreg.REG_DWORD, SZ: winreg.REG_SZ}
_TYPE_BACK = {winreg.REG_DWORD: DW, winreg.REG_SZ: SZ, winreg.REG_EXPAND_SZ: "expand_sz", winreg.REG_QWORD: "qword",
              winreg.REG_BINARY: "binary", winreg.REG_MULTI_SZ: "multi_sz"}
_TYPE_FWD = {v: k for k, v in _TYPE_BACK.items()}


class WinReg:
    """The real registry (64-bit view). Every call works on (hive, key, name)."""

    def _view(self, hive):
        return winreg.KEY_WOW64_64KEY if hive == HKLM else 0

    def get(self, hive, key, name):
        """(type, data) or None when the value does not exist."""
        try:
            with winreg.OpenKey(_ROOT[hive], key, 0, winreg.KEY_READ | self._view(hive)) as k:
                data, t = winreg.QueryValueEx(k, name)
                if t == winreg.REG_BINARY and data is not None:
                    data = data.hex()
                return _TYPE_BACK.get(t, str(t)), data
        except OSError:
            return None

    def key_exists(self, hive, key):
        try:
            winreg.OpenKey(_ROOT[hive], key, 0, winreg.KEY_READ | self._view(hive)).Close()
            return True
        except OSError:
            return False

    def set(self, hive, key, name, typ, data):
        t = _TYPE_FWD.get(typ, winreg.REG_SZ)
        if t == winreg.REG_BINARY and isinstance(data, str):
            data = bytes.fromhex(data)
        with winreg.CreateKeyEx(_ROOT[hive], key, 0, winreg.KEY_SET_VALUE | self._view(hive)) as k:
            winreg.SetValueEx(k, name, 0, t, data)

    def delete(self, hive, key, name):
        try:
            with winreg.OpenKey(_ROOT[hive], key, 0, winreg.KEY_SET_VALUE | self._view(hive)) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass

    def delete_key_if_empty(self, hive, key):
        try:
            with winreg.OpenKey(_ROOT[hive], key, 0, winreg.KEY_READ | self._view(hive)) as k:
                nsub, nval, _ = winreg.QueryInfoKey(k)
            if nsub or nval:
                return False
            winreg.DeleteKeyEx(_ROOT[hive], key, self._view(hive), 0)
            return True
        except OSError:
            return False


def _missing_keys(reg, hive, key):
    """The keys of `key`'s path that don't exist yet, deepest first (they will be created by setting a value)."""
    parts = key.split("\\")
    out = []
    for i in range(len(parts), 0, -1):
        k = "\\".join(parts[:i])
        if reg.key_exists(hive, k):
            break
        out.append(k)
    return out


def tweak_state(tw, reg):
    """'on' (Windows as it comes), 'off' (debloated) or 'partial', plus every value read."""
    vals, done = [], 0
    for v in tw["v"]:
        cur = reg.get(v["hive"], v["key"], v["name"])
        is_off = cur is not None and cur[1] == v["value"]
        done += is_off
        vals.append({**v, "now": None if cur is None else cur[1]})
    st = "off" if done == len(tw["v"]) else ("partial" if done else "on")
    return st, vals


def tweak_backup(tw, reg):
    """What is needed to undo a tweak exactly (previous values, keys that will be created). Reads only."""
    rec = {"id": tw["id"], "t": time.time(), "prev": [], "created": []}
    seen = set()
    for v in tw["v"]:
        cur = reg.get(v["hive"], v["key"], v["name"])
        rec["prev"].append({"hive": v["hive"], "key": v["key"], "name": v["name"],
                            "existed": cur is not None, "type": cur[0] if cur else None, "data": cur[1] if cur else None})
        for k in _missing_keys(reg, v["hive"], v["key"]):
            if (v["hive"], k.lower()) not in seen:
                seen.add((v["hive"], k.lower()))
                rec["created"].append({"hive": v["hive"], "key": k})
    return rec


def apply_tweak(tw, reg, save=None):
    """Debloats one tweak. Returns the backup needed to undo it exactly. `save(rec)` is called with the backup
    BEFORE anything is written; if it raises, nothing is changed. If a value can't be written, what was already
    written is put back and the error is raised."""
    rec = tweak_backup(tw, reg)
    if save:
        save(rec)
    done = {"prev": [], "created": rec["created"]}
    try:
        for v, p in zip(tw["v"], rec["prev"]):
            reg.set(v["hive"], v["key"], v["name"], v["type"], v["value"])
            done["prev"].append(p)
    except OSError:
        undo_tweak(done, reg)
        raise
    return rec


def undo_tweak(rec, reg):
    """Puts back exactly what the backup says: old values, values that did not exist, keys that were created."""
    errors = []
    for p in reversed(rec.get("prev", [])):
        try:
            if p["existed"]:
                reg.set(p["hive"], p["key"], p["name"], p["type"], p["data"])
            else:
                reg.delete(p["hive"], p["key"], p["name"])
        except OSError as e:
            errors.append(f"{p['hive']}\\{p['key']}\\{p['name']}: {e}")
    # deepest keys first
    for c in sorted(rec.get("created", []), key=lambda c: -c["key"].count("\\")):
        reg.delete_key_if_empty(c["hive"], c["key"])
    return errors


def default_tweak(tw, reg):
    """Without a backup: writes the Windows default (or removes the value when Windows doesn't have one)."""
    for v in tw["v"]:
        if v["default"] is None:
            reg.delete(v["hive"], v["key"], v["name"])
        else:
            reg.set(v["hive"], v["key"], v["name"], v["type"], v["default"])


def needs_admin(tw):
    """HKLM, and HKCU\\Software\\Policies (an ordinary user only has ReadKey there)."""
    return any(user_needs_admin(v["hive"], v["key"]) for v in tw["v"])


# =============================================================== state (what RsHub changed)
def _state_path():
    user = re.sub(r"[^A-Za-z0-9._-]", "_", os.environ.get("USERNAME") or "user")
    return os.path.join(STATE_DIR, f"state-{user}.json")


def _state():
    s = _load_json(_state_path(), {})
    s.setdefault("tweaks", {})
    s.setdefault("apps", [])
    return s


def _save_state(s):
    _save_json(_state_path(), s)


def history():
    s = _state()
    out = []
    for a in s["apps"]:
        if a.get("reinstalled"):
            continue
        out.append({"name": a.get("name"), "label": tr(a.get("label") or a.get("name") or ""), "t": a.get("t"),
                    "family": a.get("family"), "deprov": bool(a.get("deprov")),
                    "restore": restore_plan(a, bool(a.get("manifest")) and os.path.isfile(a["manifest"]))})
    return {"tweaks": list(s["tweaks"]), "apps": out}


def _broadcast():
    """Tells Explorer and the taskbar that settings changed (some pick it up without restarting)."""
    try:
        import ctypes
        res = ctypes.c_ulong()
        for what in ("TraySettings", "Policy", "ImmersiveColorSet"):
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, what, 0x0002, 500, ctypes.byref(res))
    except Exception:
        pass


def _tweak_by_id():
    return {t["id"]: t for t in TWEAKS}


def tweaks_list(reg=None):
    reg = reg or WinReg()
    st = _state()
    admin = _admin()
    out = []
    for tw in TWEAKS:
        if not present(tw):
            continue
        state, vals = tweak_state(tw, reg)
        out.append({"id": tw["id"], "g": tw["g"], "t": tr(tw["t"]), "d": tr(tw["d"]), "rec": bool(tw.get("rec")),
                    "explorer": bool(tw.get("explorer")), "admin": needs_admin(tw), "state": state,
                    "ours": tw["id"] in st["tweaks"],
                    "vals": [{"path": f"{v['hive']}\\{v['key']}", "name": v["name"] or "(Default)", "now": v["now"],
                              "to": v["value"]} for v in vals]})
    return {"items": out, "groups": [{"id": g, "n": tr(n)} for g, n in GROUPS], "admin": admin}


def tweak_undo(tid, reg=None):
    """Undo of one tweak: the backup if RsHub made the change, otherwise the Windows default."""
    reg = reg or WinReg()
    tw = _tweak_by_id().get(tid)
    if not tw:
        raise ValueError(tid)
    if needs_admin(tw) and not _admin():
        return {"needAdmin": True}
    with _state_lock:
        st = _state()
        rec = st["tweaks"].pop(tid, None)
        errors = undo_tweak(rec, reg) if rec else (default_tweak(tw, reg) or [])
        if rec and errors:   # not everything went back: keep the backup (the user's own values) to try again
            st["tweaks"][tid] = rec
        try:
            _save_state(st)
        except OSError as e:
            errors.append(tr("The undo was done but the record couldn't be updated ({error}).", error=e))
    if not isinstance(reg, WinReg):
        return {"ok": not errors, "errors": errors}
    _broadcast()
    return {"ok": not errors, "errors": errors, "explorer": bool(tw.get("explorer"))}


# =============================================================== jobs: apply, reinstall, undo all
def _ps_ok(script, marker, timeout=180):
    out = ps(script, timeout=timeout)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    return marker in lines, "\n".join(lines[-6:])


def apply(job, plan, reg=None):
    """plan: {"apps": [package full names], "deprov": bool, "tweaks": [ids]}. Apps are removed for the current
    user (and for new users if deprov and administrator); tweaks are applied with a backup each."""
    reg = reg or WinReg()
    plan = plan or {}
    admin = _admin()
    apps, tids = list(plan.get("apps") or []), list(plan.get("tweaks") or [])
    deprov = bool(plan.get("deprov")) and admin
    res = {"removed": [], "failed": [], "tweaks": [], "tweakFailed": [], "skippedAdmin": [], "explorer": False}
    total = max(1, len(apps) + len(tids))
    n = 0
    if apps:
        job.progress(0, tr("Reading the installed apps…"), force=True)
        inst = {r["PackageFullName"]: r for r in _installed()}
        prov = _provisioned() if admin else None
    for full in apps:
        job.check()
        r = inst.get(full)
        n += 1
        if not r:
            res["failed"].append({"id": full, "error": tr("It's no longer installed.")})
            continue
        c = catalog_entry(r["Name"])
        if not c or c["g"] == "keep":
            res["failed"].append({"id": full, "error": tr("RsHub doesn't remove this app.")})
            continue
        label = tr(c["n"])
        job.progress((n - 1) / total, tr("Removing {name}…", name=label), current=label)
        job.log(tr("Removing {name}…", name=label))
        loc = r.get("InstallLocation") or ""
        rec = {"id": full, "name": r["Name"], "family": r.get("PackageFamilyName") or "", "label": c["n"],
               "version": r.get("Version") or "", "store": c.get("store") or "", "gone": bool(c.get("gone")),
               "loc": loc, "manifest": os.path.join(loc, "AppxManifest.xml") if loc else "",
               "wasProv": (r["Name"] in prov) if prov is not None else None, "deprov": deprov, "t": time.time()}
        # the record that lets the app be brought back is saved BEFORE removing it; without it, nothing is removed
        with _state_lock:
            st = _state()
            before = list(st["apps"])
            st["apps"] = [a for a in before if a.get("name") != rec["name"]] + [rec]
            try:
                _save_state(st)
            except OSError as e:
                msg = tr("Not removed: its restore record couldn't be saved ({error}).", error=e)
                job.log(f"{label}: {msg}")
                res["failed"].append({"id": full, "error": msg})
                continue
        try:
            ok, out = _ps_ok(remove_script(full, r["Name"], deprov), "removed")
        except ValueError as e:
            ok, out = False, str(e)
        if not ok:
            with _state_lock:  # it is still installed: drop its record again
                st = _state()
                st["apps"] = before
                try:
                    _save_state(st)
                except OSError:
                    pass
            job.log(out)
            res["failed"].append({"id": full, "error": out.splitlines()[-1] if out else tr("Windows didn't remove it.")})
            continue
        rec["restore"] = restore_plan(rec, os.path.isfile(rec["manifest"]) if rec["manifest"] else False)
        res["removed"].append({"id": full, "name": r["Name"], "label": label, "restore": rec["restore"]})
        job.log(tr("{name} removed.", name=label))
    tw_by = _tweak_by_id()
    for tid in tids:
        job.check()
        n += 1
        tw = tw_by.get(tid)
        if not tw:
            continue
        if needs_admin(tw) and not admin:
            res["skippedAdmin"].append(tid)
            continue
        job.progress((n - 1) / total, tr(tw["t"]), current=tr(tw["t"]))
        with _state_lock:
            st = _state()
            added = []
            failed_save = []

            def save(rec):
                # keep the oldest backup: it is the state before RsHub touched anything
                if tid not in st["tweaks"]:
                    st["tweaks"][tid] = rec
                    added.append(tid)
                try:
                    _save_state(st)
                except OSError as e:
                    failed_save.append(e)
                    raise
            try:
                apply_tweak(tw, reg, save)
            except OSError as e:
                if failed_save:
                    e = tr("Not changed: its backup couldn't be saved ({error}).", error=failed_save[0])
                elif added:  # the values were put back: the backup is not needed
                    st["tweaks"].pop(tid, None)
                    try:
                        _save_state(st)
                    except OSError:
                        pass
                res["tweakFailed"].append({"id": tid, "error": str(e)})
                job.log(f"{tr(tw['t'])}: {e}")
                continue
        res["tweaks"].append(tid)
        res["explorer"] = res["explorer"] or bool(tw.get("explorer"))
        job.log(tr("Changed: {name}", name=tr(tw["t"])))
    if tids and isinstance(reg, WinReg):
        _broadcast()
    job.progress(1, tr("Done"))
    return res


def reinstall(job, name):
    """Brings back an app removed with RsHub: registers its files if they are still on disk, otherwise
    installs it from the Microsoft Store (winget), otherwise opens its Store page."""
    with _state_lock:
        st = _state()
        rec = next((a for a in st["apps"] if a.get("name") == name and not a.get("reinstalled")), None)
    if not rec:
        raise ValueError(name)
    label = tr(rec.get("label") or name)
    how = restore_plan(rec, bool(rec.get("manifest")) and os.path.isfile(rec["manifest"]))
    job.progress(.05, tr("Reinstalling {name}…", name=label), force=True)
    ok, way, out = False, how, ""
    if how == "register":
        job.log(f"> Add-AppxPackage -Register \"{rec['manifest']}\"")
        try:
            ok, out = _ps_ok(register_script(rec["manifest"]), "ok")
        except ValueError as e:
            out = str(e)
        if not ok:
            job.log(out)
            how = "store" if not rec.get("gone") else "none"
    if not ok and how == "store" and rec.get("store"):
        way = "winget"
        args = winget_args(rec["store"])
        job.log("> " + " ".join(args))
        try:
            from .ps import run
            code, out = run(args, on_line=lambda ln: ln.strip() and job.log(ln), job=job)
            ok = code == 0
        except OSError as e:
            out = str(e)
    if not ok and how == "store" and rec.get("family"):
        open_store(rec["family"])
        job.progress(1, tr("Done"))
        return {"ok": False, "store": True, "label": label}
    if ok:
        with _state_lock:
            st = _state()
            for a in st["apps"]:
                if a.get("name") == name:
                    a["reinstalled"] = time.time()
            _save_state(st)
    job.progress(1, tr("Done"))
    return {"ok": ok, "way": way, "gone": how == "none", "label": label, "msg": (out or "")[-300:]}


def open_store(family):
    if not re.fullmatch(r"[A-Za-z0-9._\-]+_[a-z0-9]{13}", family or ""):
        raise ValueError(family)
    os.startfile(f"ms-windows-store://pdp/?PFN={family}")
    return True


def undo_all(job, reg=None):
    """Undoes every tweak RsHub applied and reinstalls the apps it removed where that is possible."""
    reg = reg or WinReg()
    admin = _admin()
    with _state_lock:
        st = _state()
    tw_by = _tweak_by_id()
    tids = list(st["tweaks"])
    apps = [a for a in st["apps"] if not a.get("reinstalled")]
    total = max(1, len(tids) + len(apps))
    res = {"tweaks": [], "tweakFailed": [], "skippedAdmin": [], "apps": [], "appsStore": [], "appsGone": [], "appsFailed": []}
    n = 0
    for tid in tids:
        n += 1
        tw = tw_by.get(tid)
        if tw and needs_admin(tw) and not admin:
            res["skippedAdmin"].append(tid)
            continue
        job.progress(n / total, tr(tw["t"]) if tw else tid)
        with _state_lock:
            s = _state()
            rec = s["tweaks"].pop(tid, None)
            errs = undo_tweak(rec, reg) if rec else []
            if rec and errs:   # kept, so a later undo still has the values to put back
                s["tweaks"][tid] = rec
            _save_state(s)
        (res["tweakFailed"] if errs else res["tweaks"]).append(tid)
    for a in apps:
        n += 1
        job.check()
        label = tr(a.get("label") or a["name"])
        how = restore_plan(a, bool(a.get("manifest")) and os.path.isfile(a["manifest"]))
        job.progress(n / total, tr("Reinstalling {name}…", name=label))
        if how == "none":
            res["appsGone"].append(label)
            continue
        if how == "store" and not a.get("store"):
            res["appsStore"].append({"label": label, "family": a.get("family")})
            continue
        r = reinstall(job, a["name"])
        if r.get("ok"):
            res["apps"].append(label)
        elif r.get("store"):
            res["appsStore"].append({"label": label, "family": a.get("family")})
        else:
            res["appsFailed"].append(label)
    if tids and isinstance(reg, WinReg):
        _broadcast()
    job.progress(1, tr("Done"))
    return res

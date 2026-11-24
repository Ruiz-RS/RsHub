"""settings.json: reading, writing, the changes made through the API, reset, the installer's --setup keys and the
one-time file migration that has to happen before the first read. Nothing here touches a window."""
import glob
import json
import os
import shutil
import threading
import time
from pathlib import Path

from . import config

_lock = threading.Lock()  # one read-modify-write of the file at a time

# what "Reset settings" keeps: the user's own data (routines and their log, history, kits, ignored updates,
# exclusions, test results), not preferences
KEEP_ON_RESET = ("autopilotRoutines", "autopilotLog", "autopilotSchema", "autopilotNotify", "activity", "pending",
                 "hangarKits", "updIgnored", "updSchedule", "cleanExclusions", "testerKbMissing", "cpsBest", "uiPort",
                 "language")

# the choices the installer may write with --setup key=value, and their type
SETUP_KEYS = {"language": str, "startWithWindows": bool, "alwaysAdmin": bool, "widgetWasOpen": bool, "autoUpdate": bool}


# ---------------------------------------------------------------- migrations (run once, before the first read)
def migrate_store_python_data():
    """Python from the Microsoft Store keeps %APPDATA% in a private copy (LocalCache, Roaming). When the
    installed RsHub.exe starts for the first time and the real folder has no settings, it brings over
    the settings and the Autopilot log a source run left there, so nothing is lost when switching."""
    if os.path.exists(config.SETTINGS) or config.PORTABLE:
        return
    pkgs = Path(os.environ.get("LOCALAPPDATA") or "") / "Packages"
    try:
        cands = [pkgs / d.name / "LocalCache" / "Roaming" / "RsHub" for d in os.scandir(pkgs)
                 if d.name.startswith("PythonSoftwareFoundation.Python.")]
    except OSError:
        return
    cands = [c for c in cands if (c / "settings.json").is_file()]
    if not cands:
        return
    src = max(cands, key=lambda c: (c / "settings.json").stat().st_mtime)
    try:
        data = Path(config.DATA_DIR)
        data.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "settings.json", config.SETTINGS)
        if (src / "autopilot").is_dir():
            shutil.copytree(src / "autopilot", data / "autopilot", dirs_exist_ok=True)
    except OSError:
        pass


# the interface sounds (ui/js/core.js SH.sound): how long quitting waits for the exit sound, which fades out with the
# window at that point (the file is longer)
EXIT_SOUND_WAIT = 0.7


def plays_exit_sound(s):
    """Whether quitting plays the exit sound: sounds on (the default), the System group on, the volume above 0."""
    s = s if isinstance(s, dict) else {}
    try:
        vol = float(s.get("soundVolume", 0.4))
    except (TypeError, ValueError):
        vol = 0.6
    return s.get("sounds", True) is not False and s.get("soundSystem", True) is not False and vol > 0


# ---------------------------------------------------------------- read / write
def read():
    """Best effort: {} when the file is missing or unreadable (start path, installer, elevation checks)."""
    try:
        with open(config.SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}  # `[]` or `null` made every caller's .get() raise: no start at all


def load_strict():
    """settings.json as a dict. Missing: {}. Unreadable for a moment (antivirus, backup software): a few retries,
    then OSError. Damaged: kept as settings.json.bad and {} (so it is never silently overwritten)."""
    path = config.SETTINGS
    for i in range(6):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except ValueError:
            # keep each damaged copy (settings.json.bad-<time>, the last 3), so a second corruption can't erase the first
            try:
                shutil.copy2(path, path + ".bad-" + time.strftime("%Y%m%d-%H%M%S"))
                for old in sorted(glob.glob(glob.escape(path) + ".bad-*"))[:-3]:
                    os.remove(old)
            except OSError:
                pass
            return {}
        except OSError:
            if i == 5:
                raise
            time.sleep(.05)
    return {}


def get():
    """What the UI sees: the settings, or {} when the file can't be read right now."""
    try:
        return load_strict()
    except OSError:
        return {}


def write_json(path, data):
    """Writes through a temp file and os.replace, retrying while another program has the file open for a moment."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # a temp file of this writer's own (the installer's --setup, a second process, another thread never share it),
    # flushed to the disk before it replaces the file: a power cut right after a save (a handheld's battery running
    # out) left an empty or half-written settings.json, which the next start set aside as damaged
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        for i in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(.05)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def update(changes):
    """Merges `changes` into the file. False when the file is there but can't be read right now: never overwrite
    it with less."""
    with _lock:
        try:
            data = load_strict()
        except OSError:
            return False
        data.update(changes)
        write_json(config.SETTINGS, data)
    return True


def reset():
    """Puts the preferences back to their defaults and returns the old settings (so it can be undone). The user's
    data in the file (KEEP_ON_RESET) stays."""
    with _lock:  # read and write as one step: a change saved in between (another thread's update) was lost
        old = get()
        keep = {k: old[k] for k in KEEP_ON_RESET if k in old}
        try:
            write_json(config.SETTINGS, keep)
        except OSError:
            return old, False
    return old, True


def valid_import(data):
    """An imported settings file must be a JSON object."""
    return isinstance(data, dict)


# ---------------------------------------------------------------- the installer's --setup key=value
def parse_setup(pairs):
    """Only the keys in SETUP_KEYS are accepted; anything else is ignored."""
    changes = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        kind = SETUP_KEYS.get(k)
        if not sep or kind is None:
            continue
        changes[k] = v.strip().lower() in ("1", "true", "yes", "on") if kind is bool else v.strip()[:16]
    return changes


def apply_setup(changes):
    """Writes the installer's choices (it runs as the user who installs, not elevated, so they land in their
    profile). False when the file could not be written."""
    with _lock:
        data = read()
        data.update(changes)
        try:
            write_json(config.SETTINGS, data)
        except OSError:
            return False
    return True

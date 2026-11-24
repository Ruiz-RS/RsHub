"""Where RsHub keeps the user's data: in their profile, or next to the program in the portable version.

The portable version is the same program with a file named `RsHub.portable` next to RsHub.exe. Then
the settings, logs, history and the interface cache live in a `Data` folder beside it (so they travel on the USB
drive) and RsHub leaves nothing in Windows' startup entries or scheduled tasks.

What it saves before changing the system (registry and service backups, the uninstaller's quarantine, driver
backups) stays in ProgramData on each PC on purpose: a backup of one PC's registry belongs to that PC, so the change
can be undone there whichever drive RsHub runs from.
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
MARKER = "RsHub.portable"


def program_dir():
    """The folder RsHub.exe is in (the project folder when running from source)."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Test switches that redirect where RsHub's updates come from, where they are downloaded and checked, and what
# runs as the program. Any program running as the user can set them (HKCU\Environment), so an installed copy that
# runs elevated (the silent update task, "Always open as administrator") ignores them: they must never steer what
# runs with administrator rights. Source runs and the tests keep them.
LOCKED_WHEN_ELEVATED = ("RSHUB_UPDATE_DIR", "RSHUB_UPDATE_URL", "RSHUB_PORTABLE", "RSHUB_PORTABLE_DATA")


def _elevated():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def env(name, default=None):
    """os.environ.get(name), except the LOCKED_WHEN_ELEVATED switches in an installed copy running elevated."""
    if name in LOCKED_WHEN_ELEVATED and getattr(sys, "frozen", False) and _elevated():
        return default
    return os.environ.get(name, default)


def _portable():
    if env("RSHUB_PORTABLE") == "1":  # tests run the source as if it were portable
        return True
    return FROZEN and os.path.isfile(os.path.join(program_dir(), MARKER))


PORTABLE = _portable()


_portable_data = None


def _portable_root():
    """The portable copy whose Data folder this process uses: its own, except for the new copy that is putting itself
    in place (--finish-update <old copy> <pid>, run from the old copy's update folder): its user's data is the old
    copy's, and looking for its own made an empty Data folder inside the update."""
    if "--finish-update" in sys.argv:
        i = sys.argv.index("--finish-update")
        if len(sys.argv) > i + 1 and sys.argv[i + 1]:
            return sys.argv[i + 1]
    return program_dir()


def _portable_data_dir():
    """<program>\\Data, or, when the drive is read-only or full (a locked USB stick, a read-only share), a folder for
    this copy in %LOCALAPPDATA% so RsHub still starts and works."""
    global _portable_data
    if _portable_data:
        return _portable_data
    d = env("RSHUB_PORTABLE_DATA") or os.path.join(_portable_root(), "Data")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-test")
        with open(probe, "w") as fh:
            fh.write("1")
        os.remove(probe)
    except OSError:
        import hashlib
        tag = hashlib.sha1(os.path.normcase(program_dir()).encode("utf-8")).hexdigest()[:8]
        d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "RsHub", "Portable-" + tag)
    _portable_data = d
    return d


def data_dir():
    """Settings, logs, history: %APPDATA%\\RsHub, or <program>\\Data in the portable version."""
    if PORTABLE:
        return _portable_data_dir()
    real = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "RsHub")
    if os.environ.get("RSHUB_ISOLATED") == "1":
        return _isolated_data_dir(real)
    return real


def _isolated_data_dir(real):
    """A test copy (RSHUB_ISOLATED=1) gets its own data folder, seeded once with a copy of the real settings: it
    starts looking like the user's RsHub, but nothing it saves (settings, history, logs) reaches the real ones."""
    import shutil
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "rshub_isolated_data")
    try:
        os.makedirs(d, exist_ok=True)
        src, dst = os.path.join(real, "settings.json"), os.path.join(d, "settings.json")
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copyfile(src, dst)
    except OSError:
        pass
    return d


def local_dir():
    """Machine-local cache (the interface's browser profile): %LOCALAPPDATA%\\RsHub, or <program>\\Data\\Local."""
    if PORTABLE:
        return os.path.join(data_dir(), "Local")
    return os.path.join(os.environ.get("LOCALAPPDATA") or data_dir(), "RsHub")


def settings_file():
    return os.path.join(data_dir(), "settings.json")


def dialogs_allowed():
    """False for tests and isolated test copies (RSHUB_NO_DIALOGS=1, RSHUB_ISOLATED=1 or a test update
    folder): a message that would be a Windows message box is only written to the log, so a test never leaves a
    modal box on someone's screen."""
    e = os.environ
    return not (e.get("RSHUB_NO_DIALOGS") == "1" or e.get("RSHUB_ISOLATED") == "1" or env("RSHUB_UPDATE_DIR"))

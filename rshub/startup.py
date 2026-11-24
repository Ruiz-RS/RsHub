"""Everything that happens before the window exists: the command-line modes (installer, updater, uninstaller), the
start screen, waiting for the previous copy, administrator relaunch, the single-instance lock, the staged update and
the WebView2 check, plus the pieces that prepare the window (UI cache, port, prefetch). Heavy modules are imported
inside the functions that need them: launch time depends on it."""
import ctypes
import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import VERSION, autostart, config, elevation, instance, paths, settings
from .i18n import tr
from .log import fatal, log_to_file, phase

UI_PORT = 47822  # the interface's local web server. pywebview's default (42001) is shared by every pywebview app
WV2 = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


# ---------------------------------------------------------------- command-line modes
def run_command(argv):
    """The modes that do their job and exit instead of opening the app. True when one was handled."""
    if any(a.startswith("--multiprocessing") for a in argv[1:]):
        return True  # a worker process that freeze_support() did not catch: never boot the app from it
    if "--quit" in argv:
        instance.quit_running()
    elif "--uninstall-cleanup" in argv:
        uninstall_cleanup()
    elif "--warm" in argv:
        log_to_file()
        warm()
    elif "--setup" in argv:
        cli_setup(argv[argv.index("--setup") + 1:])
    elif "--admin-task" in argv:
        on = argv[argv.index("--admin-task") + 1:][:1] == ["on"]
        elevation.ensure_admin_task() if on else elevation.delete_admin_task()
    elif "--self-update" in argv:  # the updater (see engine/selfupdate.py): no window, never a second app
        log_to_file()
        from .engine import selfupdate
        selfupdate.run_updater()
    elif "--finish-update" in argv:  # a new portable copy putting itself in place (engine/selfupdate.py)
        from .engine import selfupdate
        i = argv.index("--finish-update")
        selfupdate.finish_update(argv[i + 1], argv[i + 2] if len(argv) > i + 2 else "0")
    elif "--update-task" in argv:  # the installer, as administrator
        from .engine import selfupdate
        on = argv[argv.index("--update-task") + 1:][:1] == ["on"]
        selfupdate.ensure_task() if on else selfupdate.delete_task()
    else:
        return False
    return True


# ---------------------------------------------------------------- how this start goes
class Launch:
    """What the command line and the environment say about this start."""

    def __init__(self, argv):
        self.hidden = "--tray" in argv
        if "--from-task" in argv and os.path.exists(elevation.START_HIDDEN):
            self.hidden = True
        self.handoff = "--wait-pid" in argv or "--from-task" in argv  # a copy the previous one is handing over to
        self.splash = not self.hidden and os.environ.get("RSHUB_NO_SPLASH") != "1"
        self.debug = "--debug" in argv
        self.splash_later = False  # another copy had its start screen up: this one shows its own once it starts


def prepare():
    """Log, language and the file migrations: the first things the main start does. Returns the Launch."""
    log_to_file()
    phase("main")
    if config.FROZEN:
        settings.migrate_store_python_data()
    from . import i18n
    i18n.set_language(os.environ.get("RSHUB_LANG") or settings.read().get("language"))
    return Launch(sys.argv)


def show_splash(launch):
    """The start screen comes first, before anything that can wait (RsHub opens with it, like FL Studio).
    Never two at once: when another copy is starting with its start screen up (RsHub opened twice), this one
    waits without its own (acquire shows it if this copy does start); a copy handing its start screen over to this one
    (administrator relaunch, update) gives it its card (splash.py)."""
    if launch.splash:
        try:
            from . import splash
            if splash.other_starting():
                launch.splash_later = True
                phase("splash deferred: another copy is starting")
                return
            splash.show()
            if launch.handoff or elevation.wants_elevation():
                splash.status(tr("Opening as administrator…"))
        except Exception as e:
            print("splash:", e)
            launch.splash = False
    phase("splash shown" if launch.splash else "no splash")


def acquire(launch):
    """Waits for the previous copy, relaunches elevated if the user chose that, and takes the single-instance lock
    (or hands over to the running copy). True when this copy must go on starting."""
    instance.wait_for_previous()
    claimed = False
    if not launch.handoff and instance.mutex_exists():  # already running (even elevated): show it, or take over if stuck
        if not instance.take_over_running_copy():
            return False
        claimed = True
    if not claimed:
        if elevation.elevate_on_start():
            return False
        if not instance.claim_single_instance(10 if launch.handoff else 0) and not instance.take_over_running_copy():
            return False
    if launch.splash_later:
        try:
            from . import splash
            splash.show()
        except Exception as e:
            print("splash:", e)
            launch.splash = False
    phase("single-instance claimed")
    return True


def start_background_work(launch):
    """Work the window does not wait for: the prefetch, the housekeeping that follows an install or update. Returns
    False when a staged update took over (the installer opens RsHub again, updated)."""
    start_prefetch()
    try:
        os.remove(elevation.START_HIDDEN)
    except OSError:
        pass
    if not launch.hidden and install_staged_update(launch.splash):
        return False
    if not config.ISOLATED:  # a test copy never takes over the user's "open with Windows" entry or administrator task
        threading.Thread(target=autostart.sync, daemon=True).start()
    if config.PORTABLE:
        from .engine import selfupdate
        threading.Thread(target=selfupdate.cleanup_after_update, daemon=True).start()
    if config.is_admin() and not config.ISOLATED:
        # an all-users install makes its task again if it is missing; leftovers of earlier versions (an unsafe task,
        # an update task made the old way) are looked for once per version, not at every start (admin_maintenance)
        threading.Thread(target=elevation.admin_maintenance, daemon=True).start()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(config.APP_ID)
    except Exception:
        pass
    return True


def warm():
    """--warm (the installer, once the files are in place, with its progress window up): loads every DLL of the
    program once. The first time Windows runs a new file, Defender / SmartScreen scan it, and that can take 15-20 s
    on the first start of a fresh install or update, with nothing on screen. Paying it here, while the installer
    shows its progress, makes the first real start as fast as any other."""
    t0 = time.monotonic()
    k32 = ctypes.windll.kernel32
    k32.LoadLibraryExW.restype = ctypes.c_void_p
    n = 0
    for root, _dirs, files in os.walk(config.base_dir()):
        for f in files:
            if f.lower().endswith((".dll", ".pyd")):
                path = os.path.join(root, f)
                h = k32.LoadLibraryExW(path, None, 0x1)  # DONT_RESOLVE_DLL_REFERENCES: mapped (and scanned), not run
                if h:
                    k32.FreeLibrary(ctypes.c_void_p(h))
                n += 1
            elif f.lower().endswith(".exe"):
                try:
                    with open(os.path.join(root, f), "rb") as fh:
                        fh.read(1 << 20)
                except OSError:
                    pass
    print(f"[warm] {n} libraries in {time.monotonic() - t0:.1f} s", flush=True)


def cli_setup(pairs):
    """--setup key=value ...: the choices made in the installer, written to this user's settings.
    The installer runs it as the user who installs (not elevated), so it lands in their profile.
    Only the keys in settings.SETUP_KEYS are accepted; anything else is ignored."""
    changes = settings.parse_setup(pairs)
    if not changes or not settings.apply_setup(changes):
        return
    if "startWithWindows" in changes:
        try:
            autostart.write(changes["startWithWindows"])
        except OSError:
            pass


def uninstall_cleanup():
    """--uninstall-cleanup: removes what RsHub registered in Windows outside its own folder."""
    # only the tasks that start THIS copy: another install of RsHub on the PC (a per-user one next to an
    # all-users one, a test copy) keeps its own
    try:
        import subprocess
        for name in (elevation.ADMIN_TASK, *elevation.LEGACY_ADMIN_TASKS):
            if elevation.task_opens_this_copy(name):
                subprocess.run(["schtasks", "/delete", "/tn", name, "/f"], capture_output=True, creationflags=0x08000000)
    except Exception:
        pass
    try:
        from .engine import selfupdate
        if selfupdate._task_exists():
            selfupdate.delete_task()
    except Exception:
        pass
    autostart.remove_if_ours()




def webview2_present():
    """Microsoft's documented check for the WebView2 runtime. The installer adds it when missing; the portable
    version can't, so it says what to do instead of failing without a word."""
    import winreg
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WV2), (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WV2),
                      (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + WV2)):
        try:
            with winreg.OpenKey(hive, key) as k:
                v = winreg.QueryValueEx(k, "pv")[0]
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def no_webview2():
    msg = tr("RsHub needs the Microsoft Edge WebView2 Runtime, and this PC doesn't have it. Download it from Microsoft now?")
    if ctypes.windll.user32.MessageBoxW(None, msg, "RsHub", 0x04 | 0x30) == 6:  # Yes/No with a warning icon; IDYES
        import webbrowser
        webbrowser.open("https://go.microsoft.com/fwlink/p/?LinkId=2124703")


def install_staged_update(splash_on):
    """A version with new features (x.Y.0) downloaded in the background is installed here, at the start, before
    the window: the start screen says "Updating to…" and RsHub opens again already updated. Never with
    "Install new features automatically" off, and never blocks the start if anything is off."""
    if not config.FROZEN or config.ISOLATED and not paths.env("RSHUB_UPDATE_URL"):
        return False
    try:
        from .engine import selfupdate
        ver = selfupdate.staged()
        if not ver:
            return False
        try:
            prefs = settings.load_strict()
        except OSError as e:  # unreadable for now: the choice is unknown, so nothing is installed and nothing is said
            print("staged update: the settings can't be read right now:", e)
            return False
        if prefs.get("autoUpdateFeatures") is False or prefs.get("skipVersion") == ver:
            return False  # off, or the user chose to skip this version after it was downloaded
        say = None
        if splash_on:
            from . import splash
            say = splash.status
        if not selfupdate.apply_staged_at_start(say=say):
            if say:
                say("")
            return False
        # the start screen stays up ("Installing the update…") until this process exits: the updater puts up the
        # same card right away, and then the installer's own progress window
        return True
    except Exception as e:
        print("staged update:", e)
        if splash_on:
            fatal(tr("The update could not be installed. RsHub will open again as it was."))
        return False




def ui_port():
    """A stable port for the interface: the page's local storage (language, small preferences) belongs to the
    address, so it must not change between starts. If ours is taken, pick a free one and remember it."""
    import socket

    def free(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False
    saved = settings.read().get("uiPort")
    for port in ([] if config.ISOLATED else ([saved] if isinstance(saved, int) else []) + [UI_PORT]):
        if free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if config.ISOLATED:
        return port
    try:
        settings.update({"uiPort": port})
    except OSError:
        pass
    return port


# WebView2 browser switches RsHub needs: the interface sounds play without waiting for a click (the start sound plays
# as the window appears). pywebview's own switch is repeated because the environment variable replaces, rather than
# adds to, the arguments pywebview gives WebView2.
BROWSER_ARGS = ("--autoplay-policy=no-user-gesture-required", "--disable-features=ElasticOverscroll")
BROWSER_ARGS_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"


def merge_browser_args(existing, extra=BROWSER_ARGS):
    """`existing` (the environment variable as it is, e.g. a test copy's --remote-debugging-port) plus every switch in
    `extra` it does not have yet. A switch already there keeps its value; --disable-features / --enable-features lists
    are joined. Order is kept: what was there first, then the new switches."""
    parts = (existing or "").split()
    for arg in extra:
        name, _, val = arg.partition("=")
        i = next((k for k, p in enumerate(parts) if p.partition("=")[0] == name), None)
        if i is None:
            parts.append(arg)
        elif name in ("--disable-features", "--enable-features") and val:
            have = [f for f in parts[i].partition("=")[2].split(",") if f]
            parts[i] = name + "=" + ",".join(have + [f for f in val.split(",") if f and f not in have])
    return " ".join(parts)


def set_browser_args():
    """Before webview.start: merged into whatever the variable already holds (never dropped)."""
    os.environ[BROWSER_ARGS_ENV] = merge_browser_args(os.environ.get(BROWSER_ARGS_ENV, ""))


def refresh_ui_cache():
    """WebView2 keeps the .js/.css in its HTTP cache: after a UI change it would keep showing the old one.
    If any UI file changes, that cache is cleared."""
    if not config.FROZEN:  # the page's one-request dictionaries (the installer's build makes them: packaging spec)
        try:
            from . import i18n
            i18n.build_bundles()
        except Exception as e:
            print("i18n bundles:", e)
    h = hashlib.sha1()
    if config.FROZEN:  # installed: the UI only changes with a new build, so the version and build time identify it
        try:
            h.update(f"{VERSION}|{os.path.getmtime(sys.executable)}".encode())
        except OSError:
            h.update(VERSION.encode())
    for root, _dirs, files in ([] if config.FROZEN else sorted(os.walk(config.UI_DIR))):
        for f in sorted(files):
            if f.endswith((".html", ".js", ".css", ".json", ".ogg")):
                p = os.path.join(root, f)
                h.update(p[len(config.UI_DIR):].encode())
                try:
                    with open(p, "rb") as fh:
                        h.update(fh.read())
                except OSError:
                    pass
    web = Path(config.WEB_DIR)
    stamp = web / "ui.version"
    try:
        with open(stamp, encoding="utf-8") as fh:
            if fh.read().strip() == h.hexdigest():
                return
    except OSError:
        pass
    for sub in (("EBWebView", "Default", "Cache"), ("EBWebView", "Default", "Code Cache")):
        shutil.rmtree(web.joinpath(*sub), ignore_errors=True)
    try:
        web.mkdir(parents=True, exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(h.hexdigest())
    except OSError:
        pass


_prefetch_started = threading.Event()


def start_prefetch():
    """Starts the prefetch once (main() calls it before the start screen so the imports overlap with it)."""
    if not _prefetch_started.is_set():
        _prefetch_started.set()
        threading.Thread(target=prefetch, daemon=True, name="prefetch").start()


def prefetch():
    """Loads, on a second thread, what the window setup below needs anyway (pywebview and its .NET bridge, the tray
    with its icon, the engine modules), so those imports overlap with the other start work instead of queueing
    after it. Everything here is only an import (or a cached drawing): failing just means it loads later."""
    try:
        import webview
        from . import chrome  # noqa: F401
        import webview.platforms.edgechromium  # noqa: F401  (loads pythonnet and WinForms)
    except Exception as e:
        print("prefetch:", e)
    try:
        from . import tray
        tray.icon_image()  # draws and caches the tray icon
        from .engine import jobs, telemetry, autopilot  # noqa: F401
    except Exception as e:
        print("prefetch:", e)
    phase("prefetched")


def let_windows_shut_down(api):
    """Closing the window normally hides it to the tray (_on_closing cancels the close). When Windows is signing out,
    restarting or shutting down, that cancel would block it ("RsHub is preventing restart"), so on SessionEnding
    RsHub marks itself as quitting and lets every close through."""
    try:
        import clr  # noqa: F401
        clr.AddReference("System")
        from Microsoft.Win32 import SystemEvents, SessionEndingEventHandler

        def ending(sender, args):
            api._quitting = True
            try:
                from .engine import autopilot
                autopilot.stop()
            except Exception:
                pass
        SystemEvents.SessionEnding += SessionEndingEventHandler(ending)
        api._session_handler = ending  # keep a reference so it is not collected
    except Exception as e:
        print("session end hook:", e)

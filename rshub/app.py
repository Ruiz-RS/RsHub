"""RsHub: the entry point. main() only wires the pieces together:

  config      constants and locations            startup    everything before the window exists
  settings    settings.json                      instance   single-instance lock and handoff
  elevation   administrator mode and its task    autostart  "open with Windows"
  winmgr      window mechanics (DPI, corners...) api        what the interface can call (pywebview js_api)
  log         [phase] timeline and the log file  jssender   ordered evaluate_js per window

The names below are re-exported because tests, the engine (autopilot, hangar read SETTINGS) and run.py use them."""
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import config, instance, startup, winmgr
from .api import Api
from .api.engine_api import attach_telemetry
from .config import APP_ID, BG, DATA_DIR, FROZEN, ISOLATED, PORTABLE, SETTINGS, UI_DIR, WEB_DIR, base_dir, is_admin  # noqa: F401
from .i18n import tr
from .instance import ask_running_to_show as _ask_running_to_show, listen_single as _listen_single  # noqa: F401
from .jssender import Sender as _Sender  # noqa: F401
from .log import LogOnce as _LogOnce, fatal as _fatal, phase as _phase, rotate_log as _rotate_log  # noqa: F401
from .startup import refresh_ui_cache  # noqa: F401
from .winmgr import panel_geometry as _panel_geometry, style_titlebar  # noqa: F401


def _drop_test_profile(wait=3.0):
    """An isolated test copy (RSHUB_ISOLATED=1) gets a WebView2 profile of its own in %TEMP%
    (rshub_isolated_<pid>, config.WEB_DIR); it is removed when that copy quits, once its WebView2 processes are
    gone, so test runs do not pile them up in the temp folder (hundreds were left, slowing every cleanup scan)."""
    import shutil
    try:
        import psutil
        kids = [k for k in psutil.Process().children(recursive=True) if k.name().lower() == "msedgewebview2.exe"]
        psutil.wait_procs(kids, timeout=wait)
    except Exception:
        pass
    if os.path.basename(WEB_DIR).startswith("rshub_isolated_"):  # never anything but that folder
        shutil.rmtree(WEB_DIR, ignore_errors=True)


def _leave():
    """This copy has nothing more to do (the running copy was shown, the elevated copy or the installer took over).
    The prefetch thread may be halfway through loading .NET (pythonnet) by now, and any normal exit, os._exit too,
    shuts that runtime down: it then failed with an unhandled .NET exception ("GIL must always be released...", exit
    code 0xE0434352), a crash report for Windows Error Reporting at every second double click, and such a copy stayed
    in the background for up to 25 s. The process is ended at once instead, with nothing left to save."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.TerminateProcess(ctypes.c_void_p(k32.GetCurrentProcess()), 0)
    except Exception:
        pass
    os._exit(0)


def main():
    if startup.run_command(sys.argv):
        return
    launch = startup.prepare()
    startup.start_prefetch()  # imports and the tray icon load while the start screen comes up
    startup.show_splash(launch)
    if not startup.acquire(launch):
        _leave()
    if not startup.start_background_work(launch):
        _leave()  # the installer (or the new portable copy) takes over and opens RsHub again, updated
    if not startup.webview2_present():
        startup.no_webview2()
        _leave()
    import webview
    _phase("webview imported")

    api = Api()
    api._visible = not launch.hidden  # before anything can show the window (a second copy's "show" via listen_single)
    _phase("api built")
    title = tr("RsHub (administrator)") if is_admin() else "RsHub"
    from .chrome import Chrome
    _phase("chrome imported")

    (win_w, win_h), win_min = winmgr.main_window_sizes()
    window = webview.create_window(
        title, url=str(Path(config.UI_DIR) / "index.html"), js_api=api,
        width=win_w, height=win_h, min_size=win_min, frameless=True, easy_drag=False, shadow=True,
        background_color=BG, text_select=False, hidden=launch.hidden,
        x=-32000 if launch.splash else None, y=-32000 if launch.splash else None,
    )
    api._attach(window)
    if launch.splash:
        api._reveal_pending = True
        t = threading.Timer(45, api._reveal)  # whatever happens, never leave only the start screen
        t.daemon = True
        t.start()

    def on_state(maxed):  # called from the window thread: never wait here
        api._js(f"window.SH && SH.emit('win', {{max: {'true' if maxed else 'false'}}})")

    api._chrome = Chrome(window, on_state)
    # with the start screen up, the window is placed and maximized when it is revealed (Api._reveal)
    window.events.shown += lambda: (None if api._reveal_pending else api._chrome.setup(start_max=True), winmgr.grant_media(window))
    window.events.closing += api._on_closing
    for ev in (window.events.minimized, window.events.restored, window.events.maximized):
        ev += api._win_state

    from .tray import Tray
    api._tray = Tray(api)

    def start_tray():  # the icon is drawn and registered off the start path (the window does not wait for it)
        try:
            api._tray.start()
        except Exception as e:  # without a tray the app still works; the X quits completely
            print("tray:", e)
            api._tray = None
    threading.Thread(target=start_tray, daemon=True, name="tray-start").start()

    _phase("tray started")
    instance.listen_single(api)

    # engine: long-task notifications and real telemetry
    from .engine import jobs
    jobs.set_emitter(lambda ev, data: api._js(f"window.SH && SH.emit({json.dumps(ev)}, {json.dumps(data)})",
                                              lossy=isinstance(data, dict) and data.get("kind") == "progress"))
    attach_telemetry(api, window)

    # Autopilot: runs the routines while the app is open, also hidden in the tray
    try:
        from .engine import autopilot
        if not ISOLATED:  # a test copy must not run the user's routines or write their Autopilot state
            autopilot.attach((lambda title, text: api._tray.notify(text, title)) if api._tray else None)
    except Exception as e:
        print("autopilot:", e)

    # freeze detector: if the window or the page stops answering, every thread's stack goes to the log once
    winmgr.watch_ui(window, lambda: max([s.busy_for() for s in list(api._senders.values())] or [0]))
    window.events.loaded += lambda: _phase("page loaded")
    _phase("engine attached")
    startup.refresh_ui_cache()
    # last before the window: it needs .NET (pythonnet), which the prefetch thread is still loading. Here the work above
    # (tray, engine, telemetry, UI cache) overlaps with that load instead of queueing after it (~0.2 s off the start)
    startup.let_windows_shut_down(api)
    startup.set_browser_args()  # sounds may play without a click (the start sound)
    _phase("window created, starting webview")
    webview.start(gui="edgechromium", debug=launch.debug, http_server=True, http_port=startup.ui_port(),
                  private_mode=False, storage_path=WEB_DIR)
    if api._tray is not None:
        api._tray.stop()
    if getattr(api, "_handoff", False):  # no gap on screen: wait (bounded) until the new copy's start screen is up
        try:
            from . import splash
            end = time.monotonic() + 5
            while time.monotonic() < end and not splash.other_showing():
                time.sleep(0.05)
        except Exception:
            pass
    if ISOLATED:
        _drop_test_profile()
    # The window is gone and settings are saved as they change. Background scans still running in thread pools
    # (winget, PowerShell...) would keep the process alive until they finish, so end it now.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)

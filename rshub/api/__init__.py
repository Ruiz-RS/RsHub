"""The API the interface sees: everything public in Api can be called from JS as window.pywebview.api.<name>.

The methods live in one mixin per area (settings, main window, widget, tray panel, engine and telemetry); this class
holds the state they share (the windows, the tray, the sampler) and the way to talk to the UI without waiting."""
import threading
import time

from .. import CODENAME, VERSION, config, winmgr
from ..jssender import Sender
from ..log import phase
from .engine_api import EngineApi
from .main_window import MainWindowApi
from .settings_api import SettingsApi
from .tray_panel import TrayPanelApi
from .widget import WidgetApi


class Api(SettingsApi, MainWindowApi, WidgetApi, TrayPanelApi, EngineApi):
    """Everything public in this class can be called from JS as window.pywebview.api.<name>."""

    def __init__(self):
        self._window = None
        self._chrome = None
        self._widget = None
        self._tray = None
        self._panel = None          # the tray panel (left click on the tray icon), created on first use
        self._panel_ready = False
        self._panel_shown = False
        self._panel_hidden_at = 0.0
        self._sampler = None
        self._visible = True
        self._minimized = False
        self._quitting = False
        self._told_tray = False
        self._lock = threading.Lock()
        self._senders = {}          # window -> Sender
        self._widget_pos_timer = None

    def _attach(self, window):
        self._window = window

    # ---------- information ----------
    def info(self):
        # adminTask: "Always open as administrator" opens without the Windows prompt (an all-users install's task);
        # False: Windows asks at each start (per-user, portable or source copies: elevation.uses_admin_task)
        from .. import elevation
        return {"version": VERSION, "codename": CODENAME, "admin": config.is_admin(), "portable": config.PORTABLE,
                "adminTask": elevation.uses_admin_task()}

    _reveal_pending = False

    def _reveal(self):
        """The start screen gives way to the window: the window comes up already painted and the card fades out on top
        of it (a crossfade). The page is NOT faded in from transparent here: that left a fraction of a second of empty
        dark window between the card and the interface, seen as a blink."""
        if not self._reveal_pending:
            return
        self._reveal_pending = False
        try:  # from off screen to the primary monitor, where show_main maximizes it
            wa = winmgr.work_area()
            self._window.move(wa.left + 60, wa.top + 40)
        except Exception as e:
            print("reveal:", e)
        self.show_main()
        try:
            from .. import splash
            threading.Timer(.22, splash.close).start()  # the maximized window gets a frame or two to repaint first
        except Exception:
            pass

    def phase(self, name):
        """The UI reports its own steps (boot data loaded...) into the start timeline in the log."""
        phase("ui: " + str(name)[:40])
        return True

    def ui_ready(self):
        """The interface finished its first frame: logs how long the start took, from the process launch. Answers
        whether the window is on screen now (False when it started hidden in the tray): the UI plays the start sound."""
        phase("ui_ready")
        self._reveal()
        phase("revealed")
        shown = bool(self._visible)
        try:
            import psutil
            secs = time.time() - psutil.Process().create_time()
            print(f"[startup] interface ready in {secs:.2f} s ({'installed' if config.FROZEN else 'source'}, "
                  f"{'administrator' if config.is_admin() else 'normal'})", flush=True)
        except Exception:
            pass
        return shown

    def _js(self, code, target=None, lossy=False):
        """Notifies the UI WITHOUT waiting, and in order (one sender thread per window)."""
        w = target or self._window
        if w is None:
            return
        with self._lock:
            sd = self._senders.get(w)
            if sd is None:
                sd = self._senders[w] = Sender(w)
        sd.put(code, lossy)

    def _sender_drop(self, w):
        with self._lock:
            sd = self._senders.pop(w, None)
        if sd is not None:
            sd.stop()
        tr = self.__dict__.get("_tele_runners", {}).pop(w, None)  # the closed window's telemetry runner and thread
        if tr is not None:
            tr.stop()

    def log_js_error(self, msg):
        """UI errors go to the log (rshub.log), to help diagnose problems."""
        print("[ui]", str(msg)[:2000], flush=True)
        return True


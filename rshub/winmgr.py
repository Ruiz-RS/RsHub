"""Window mechanics that need no app state: DPI scale, where the tray panel goes, rounded corners, the dark title bar,
camera and microphone permission, WebView2 visibility, tool-window styles and always-on-top. Everything here takes
the window (or its handle) it works on; which window is which lives in the Api."""
import ctypes
import threading
import time

from . import config


def work_area():
    """The primary monitor's work area (device pixels), or None."""
    try:
        import ctypes.wintypes as wt
        wa = wt.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)  # SPI_GETWORKAREA
        return wa
    except Exception:
        return None


# The main window in logical px (pywebview multiplies them by the scale). Below 1100 x 700 CSS px the interface uses
# its compact layout (the page scrolls), so the window may be smaller than that.
MAIN_SIZE, MAIN_MIN = (1600, 960), (1024, 576)


def main_window_sizes(work=None, scale=None):
    """(size, min_size) of the main window in logical px, both inside the primary monitor's work area. A minimum
    larger than the screen is never allowed: 1100 x 700 at 150 % on a handheld's 1080p screen is 1650 x 1050 device
    px, taller than its 1008 px work area, and Windows would keep the window that tall, its bottom (the dock with it)
    off the screen."""
    if work is None:
        wa = work_area()
        work = (wa.right - wa.left, wa.bottom - wa.top) if wa else None
    if scale is None:
        scale = sys_scale()
    if not work or work[0] <= 0 or work[1] <= 0 or not scale or scale <= 0:
        return MAIN_SIZE, MAIN_MIN
    ww, wh = int(work[0] / scale), int(work[1] / scale)
    size = (min(MAIN_SIZE[0], ww), min(MAIN_SIZE[1], wh))
    return size, (min(MAIN_MIN[0], ww), min(MAIN_MIN[1], wh))


def tool_window_style(hwnd):
    """Tool window: no taskbar button, no Alt+Tab, shows on every virtual desktop. Raises on failure."""
    u = ctypes.windll.user32
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x00000080, 0x00040000
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    ex = u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    u.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)


def keep_topmost(hwnd, alive, name="widget-top"):
    """A thread puts the window back on top every so often (without activating or moving it) while alive() says so.
    The taskbar is also "always on top" and, when touched, it jumps in front of everything."""
    u = ctypes.windll.user32
    HWND_TOPMOST = ctypes.c_void_p(-1)
    FLAGS = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER

    def keep():
        while alive():
            try:
                u.SetWindowPos(ctypes.c_void_p(hwnd), HWND_TOPMOST, 0, 0, 0, 0, FLAGS)
            except Exception:
                break
            time.sleep(0.35)
    threading.Thread(target=keep, daemon=True, name=name).start()


def cursor_monitor_geometry(pw, ph):
    """Where the tray panel goes right now: (x, y, w, h) in device pixels, from the cursor and its monitor."""
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    pt = wt.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    mon = u.MonitorFromPoint(pt, 2)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    u.GetMonitorInfoW(mon, ctypes.byref(mi))
    return panel_geometry((pt.x, pt.y), rect(mi.rcWork), rect(mi.rcMonitor), sys_scale(), pw, ph)


def show_topmost(form, x, y, w, h, hand_foreground_from=None):
    """Shows a WinForms window topmost at that rectangle and gives it the foreground (window thread)."""
    u = ctypes.windll.user32
    hwnd = form.Handle.ToInt32()
    if hand_foreground_from:  # the tray click lets this process take the foreground: hand it to the panel
        u.SetForegroundWindow(ctypes.c_void_p(hand_foreground_from))
    u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), x, y, w, h, 0x0040)  # TOPMOST, SHOWWINDOW
    form.Show()
    form.Activate()
    u.SetForegroundWindow(ctypes.c_void_p(hwnd))


def _dump_threads(title):
    """Every Python thread's stack into the log, so a freeze reported by a user says where it was waiting."""
    import sys
    import traceback
    names = {t.ident: t.name for t in threading.enumerate()}
    lines = [f"[hang] {title}"]
    for ident, frame in sys._current_frames().items():
        lines.append(f"--- thread {names.get(ident, ident)}")
        lines.extend(x.rstrip("\n") for x in traceback.format_stack(frame)[-10:])
    print("\n".join(lines), flush=True)


def watch_ui(window, busy_fn=None, every=2.0, limit=8.0):
    """Freeze detector: posts a no-op to the window thread every `every` seconds. If the window does not run it
    within `limit` seconds (Windows would call it "not responding"), or an evaluate_js has waited that long for the
    page, every thread's stack goes to the log once per freeze. Costs nothing while the window is healthy."""
    def run():
        form = None
        while form is None:
            form = getattr(window, "native", None)
            time.sleep(0.5)
        from System import Action  # (only once the window exists: .NET may still be loading before that)
        stuck = False
        while True:
            ev = threading.Event()
            try:
                form.BeginInvoke(Action(ev.set))
            except Exception:
                try:
                    gone = bool(form.IsDisposed)
                except Exception:
                    gone = True
                if gone:
                    return  # the window is gone
                time.sleep(every)  # its handle is not made yet (a slow start): it used to stop watching for good
                continue
            ok = ev.wait(limit)
            try:
                busy = busy_fn() if busy_fn is not None else 0
            except Exception:
                busy = 0
            if (not ok or busy > limit) and not stuck:
                stuck = True
                _dump_threads("the window thread has not answered for %.0f s" % limit if not ok
                              else "an evaluate_js has waited %.0f s for the page" % busy)
            elif ok and busy <= limit and stuck:
                stuck = False
                print("[hang] the window answers again", flush=True)
            time.sleep(every)
    threading.Thread(target=run, daemon=True, name="ui-watchdog").start()


def wv_visible(window, on, trim=False):
    """Tells WebView2 whether the window can be seen. A WebView2 whose window is hidden in the tray keeps
    painting and running requestAnimationFrame at full speed (measured: 11% GPU and over 100% CPU for a window
    nobody sees) unless its controller gets IsVisible = False. The WinForms control keeps that controller in a
    private field, and does not update it when the form is hidden.
    trim: the window is put away for a while (the main window hidden in the tray): WebView2 is also asked to keep
    its memory low (MemoryUsageTargetLevel.Low, Microsoft's advice for an inactive WebView), and back to Normal as
    soon as trim is False again. Scripts keep running either way."""
    if window is None:
        return
    try:
        from System import Action
        from System.Reflection import BindingFlags
        form = window.native

        def apply():
            try:
                wv = form.webview
                f = wv.GetType().GetField("_coreWebView2Controller", BindingFlags.NonPublic | BindingFlags.Instance)
                c = f.GetValue(wv) if f is not None else None
                if c is not None and c.IsVisible != on:
                    c.IsVisible = on
            except Exception as e:
                print("webview visibility:", e)
            _memory_target(form, trim)
        form.BeginInvoke(Action(apply))
    except Exception:
        pass


def _memory_target(form, low):
    """WebView2's memory target (window thread): Low while put away, Normal otherwise. A runtime without it is left
    as it is."""
    try:
        core = form.webview.CoreWebView2
        if core is None:
            return
        from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level
        want = Level.Low if low else Level.Normal
        if core.MemoryUsageTargetLevel != want:
            core.MemoryUsageTargetLevel = want
    except Exception:
        pass


TIP_CLSID = "{4CE576FA-83DC-4F88-951C-9D0782B4E376}"   # UIHostNoLaunch: the running TabTip, never a new one
TIP_IID = "{37C994E7-432B-4834-A2F7-DCE1F13B834B}"     # ITipInvocation


def show_touch_keyboard():
    """Windows' touch keyboard, for a controller user who opens a text field (a handheld has no keys). Asks the
    running TabTip through ITipInvocation::Toggle; when TabTip is not running it is started instead (it comes up with
    its keyboard). Never raises; True when Windows took the request. Only the interface's controller mode calls it."""
    import os
    try:
        ole32 = ctypes.windll.ole32

        class GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        def guid(text):
            g = GUID()
            if ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g)) != 0:
                raise OSError("bad GUID")
            return g
        clsid, iid = guid(TIP_CLSID), guid(TIP_IID)
        ole32.CoInitializeEx(None, 2)  # already initialised in another mode is fine: the call below works either way

        def create():
            obj = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x4, ctypes.byref(iid), ctypes.byref(obj))  # local server
            return obj if hr == 0 and obj.value else None
        obj = create()
        if obj is None:
            base = os.environ.get("CommonProgramW6432") or os.environ.get("CommonProgramFiles") or r"C:\Program Files\Common Files"
            exe = os.path.join(base, "microsoft shared", "ink", "TabTip.exe")
            if not os.path.isfile(exe):
                return False
            os.startfile(exe)
            return True
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        toggle = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(vtbl[3])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        try:
            return toggle(obj, ctypes.c_void_p(ctypes.windll.user32.GetDesktopWindow())) == 0
        finally:
            release(obj)
    except Exception as e:
        print("touch keyboard:", e, flush=True)
        return False


def sys_scale():
    """The scale this process renders at. pywebview makes it system-DPI aware: every window, on any monitor, is laid
    out at the primary monitor's scale (Windows stretches it on the others), so that is the one to size with."""
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96
    except Exception:
        return 1.0


def rect(r):
    return (r.left, r.top, r.right, r.bottom)


def panel_geometry(pt, work, mon, k, pw, ph):
    """Where the tray panel goes, in device pixels: (x, y, w, h). pt = the click, work/mon = (l, t, r, b) of the work
    area and of the monitor under it. This process is system-DPI aware: the page renders at the system scale k on
    every monitor and the coordinates Windows gives (cursor, monitors) are in that same space, so the panel is sized
    with k, not with the monitor's own scale. The taskbar is the edge where the work area is smaller than the monitor
    (any edge, any monitor); with an auto-hidden taskbar the edge closest to the click is used."""
    w, h, m = int(pw * k), int(ph * k), int(10 * k)
    wl, wt_, wr, wb = work
    ml, mt, mr, mb = mon
    gaps = {"bottom": mb - wb, "top": wt_ - mt, "right": mr - wr, "left": wl - ml}
    edge = max(gaps, key=gaps.get)
    if gaps[edge] <= 0:
        near = {"bottom": mb - pt[1], "top": pt[1] - mt, "right": mr - pt[0], "left": pt[0] - ml}
        edge = min(near, key=near.get)
    if edge == "bottom":
        x, y = pt[0] - w // 2, wb - h - m
    elif edge == "top":
        x, y = pt[0] - w // 2, wt_ + m
    elif edge == "right":
        x, y = wr - w - m, pt[1] - h // 2
    else:
        x, y = wl + m, pt[1] - h // 2
    x = max(wl + m, min(x, wr - w - m))
    y = max(wt_ + m, min(y, wb - h - m))
    return x, y, w, h


def on_screen(x, y):
    """Is this point (device pixels) on a connected monitor?"""
    try:
        import ctypes.wintypes as wt
        return bool(ctypes.windll.user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 0))
    except Exception:
        return True


def round_corners(window):
    """Windows 11 rounded corners for frameless windows, with a subtle edge drawn by Windows
    (the widget UI draws no border of its own, so there is no double frame at the corners)."""
    try:
        hwnd = window.native.Handle.ToInt32()
        dwm = ctypes.windll.dwmapi
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        border = ctypes.c_int(0x00473A2C)  # #2C3A47 in BGR
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)
    except Exception:
        pass


def grant_media(window):
    """Camera and microphone for RsHub's own UI (Device Tester) without the browser prompt.
    The only thing that can block them is Windows privacy, and the app links to those settings."""
    state = {"done": False}

    def hook():
        """Runs on the window thread (WebView2 can only be touched from there). Returns True if it got hooked."""
        if state["done"]:
            return True
        form = window.native
        core = form.webview.CoreWebView2
        if core is None:
            return False
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind as K, CoreWebView2PermissionState as St

        def on_perm(sender, args):
            local = str(args.Uri).startswith(("http://127.0.0.1", "http://localhost"))
            if local and args.PermissionKind in (K.Camera, K.Microphone, K.ClipboardRead, K.Notifications):
                args.State = St.Allow
        core.PermissionRequested += on_perm
        state["done"] = True
        return True

    def wait_and_hook():
        import time
        from System import Func, Boolean
        form = window.native
        for _ in range(100):
            try:
                if form.Invoke(Func[Boolean](hook)):
                    return
            except Exception as e:
                print("permissions:", e)
                return
            time.sleep(0.15)
    threading.Thread(target=wait_and_hook, daemon=True).start()


def style_titlebar(window):
    """Dark title bar in the same color as the background (Windows 11)."""
    try:
        hwnd = window.native.Handle.ToInt32()
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)  # dark mode
        r, g, b = int(config.BG[1:3], 16), int(config.BG[3:5], 16), int(config.BG[5:7], 16)
        color = ctypes.c_int(r | (g << 8) | (b << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)  # title background
        text = ctypes.c_int(0x00F6EDE8)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), 4)  # title text
    except Exception:
        pass

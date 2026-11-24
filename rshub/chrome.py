"""Custom window frame: no Windows title bar, but with native move, resize, maximize and snap.
Windows' own loops are used (WM_NCLBUTTONDOWN with HTCAPTION / HTLEFT...) so moving and resizing
are as smooth as in a normal window."""
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

WM_NCLBUTTONDOWN = 0x00A1
HT = {"move": 2, "w": 10, "e": 11, "n": 12, "nw": 13, "ne": 14, "s": 15, "sw": 16, "se": 17}
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWA_BORDER_COLOR = 33, 34
ROUND, NO_ROUND = 2, 1
BORDER = 0x00382A1E  # #1E2A38 in BGR: a subtle edge around the window
BORDER_NONE = 0xFFFFFFFE


def _dwm(hwnd, attr, value):
    v = ctypes.c_int(value)
    try:
        dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
    except Exception:
        pass


class Chrome:
    def __init__(self, window, on_state=None):
        self.window = window
        self.max = False
        self.normal = None  # (x, y, w, h) before maximizing
        self.on_state = on_state or (lambda maxed: None)
        self._guard = False

    # ---------- utilities ----------
    @property
    def form(self):
        return self.window.native

    @property
    def hwnd(self):
        return self.form.Handle.ToInt32()

    def _ui(self, fn, wait=False):
        from System import Action
        (self.form.Invoke if wait else self.form.BeginInvoke)(Action(fn))

    def _work_area(self):
        from System.Windows.Forms import Screen
        return Screen.FromHandle(self.form.Handle).WorkingArea

    # ---------- when shown ----------
    def setup(self, start_max=True):
        if getattr(self, "_done", False):
            return
        self._done = True

        def go():
            _dwm(self.hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ROUND)
            _dwm(self.hwnd, DWMWA_BORDER_COLOR, BORDER)
            from System import EventHandler
            self.form.Resize += EventHandler(self._on_resize)
            if start_max:
                self._maximize()
        self._ui(go)

    def _on_resize(self, sender, args):
        """Windows snap (drag to the top, Win+↑) does a "real" maximize that would cover the taskbar:
        it is turned into our own maximize, which respects the work area."""
        from System.Windows.Forms import FormWindowState
        if self._guard:
            return
        if self.form.WindowState == FormWindowState.Maximized:
            self._guard = True
            try:
                b = self.form.RestoreBounds
                self.form.WindowState = FormWindowState.Normal
                self.normal = (b.X, b.Y, b.Width, b.Height)
                self._maximize(keep_normal=True)
            finally:
                self._guard = False

    def _maximize(self, keep_normal=False):
        wa = self._work_area()
        if not keep_normal or self.normal is None:
            b = self.form.Bounds
            if not self.max:
                self.normal = (b.X, b.Y, b.Width, b.Height)
        self._guard = True
        try:
            self.form.SetBounds(wa.X, wa.Y, wa.Width, wa.Height)
        finally:
            self._guard = False
        self.max = True
        _dwm(self.hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, NO_ROUND)
        _dwm(self.hwnd, DWMWA_BORDER_COLOR, BORDER_NONE)
        self.on_state(True)

    def _restore(self, center_on_cursor=False):
        wa = self._work_area()
        x, y, w, h = self.normal or (wa.X + wa.Width // 8, wa.Y + wa.Height // 10, wa.Width * 3 // 4, wa.Height * 4 // 5)
        if center_on_cursor:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            x = max(wa.X, min(pt.x - w // 2, wa.X + wa.Width - w))
            y = max(wa.Y, pt.y - 22)
        self._guard = True
        try:
            self.form.SetBounds(x, y, w, h)
        finally:
            self._guard = False
        self.max = False
        _dwm(self.hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ROUND)
        _dwm(self.hwnd, DWMWA_BORDER_COLOR, BORDER)
        self.on_state(False)

    # ---------- actions called by the UI ----------
    @staticmethod
    def _button_down():
        """Windows' move/resize loop must only start while the button is still down;
        if it was already released, the window would stick to the cursor."""
        return bool(user32.GetAsyncKeyState(0x01) & 0x8000)

    def drag(self):
        def go():
            if not self._button_down():
                return
            if self.max:
                self._restore(center_on_cursor=True)
            user32.ReleaseCapture()
            user32.SendMessageW(self.hwnd, WM_NCLBUTTONDOWN, HT["move"], 0)
        self._ui(go)

    def resize(self, edge):
        if self.max or edge not in HT or edge == "move":
            return

        def go():
            if not self._button_down():
                return
            user32.ReleaseCapture()
            user32.SendMessageW(self.hwnd, WM_NCLBUTTONDOWN, HT[edge], 0)
        self._ui(go)

    def toggle_max(self):
        self._ui(lambda: self._restore() if self.max else self._maximize(), wait=True)
        return self.max

    def minimize(self):
        self.window.minimize()

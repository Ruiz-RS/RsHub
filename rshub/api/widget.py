"""The always-on-top widget window."""
import ctypes
import json
import threading
from pathlib import Path

from .. import config, winmgr
from ..i18n import tr


class WidgetApi:
    def widget_toggle(self, width=400, height=190):
        """Opens or closes the widget. Returns True if it ends up open."""
        import webview
        if self._widget is not None:
            self.widget_close()
            return False
        w, h = max(160, min(1100, int(width or 400))), max(32, min(600, int(height or 190)))
        x = y = None
        saved = self._widget_saved_pos(w)
        if saved:
            x, y = saved
        else:
            try:
                wa = winmgr.work_area()  # (primary)
                dpi = winmgr.sys_scale()
                x, y = int(wa.right / dpi) - w - 28, int(wa.top / dpi) + 72
            except Exception:
                try:
                    scr = webview.screens[0]
                    x, y = scr.width - w - 28, 72
                except Exception:
                    pass
        self._widget = webview.create_window(
            tr("RsHub widget"), url=str(Path(config.UI_DIR) / "widget.html"), js_api=self,
            width=w, height=h, x=x, y=y, frameless=True, easy_drag=True, on_top=True, min_size=(120, 30),
            resizable=False, background_color=config.BG, focus=False,
        )
        wref = self._widget
        self._widget.events.closed += lambda: self._widget_gone(wref)
        self._widget.events.shown += lambda: (winmgr.round_corners(wref), self._widget_keep_on_top(wref), self._widget_track(wref))
        self.settings_set({"widgetWasOpen": True})
        self._tele_refresh()
        self._tray and self._tray.refresh()
        self._js("SH.emit('widget', {on: true})")
        return True

    def _widget_keep_on_top(self, w):
        """The taskbar is also "always on top" and, when touched, it jumps in front of everything.
        - Tool window: shows on every virtual desktop and does not appear in Alt+Tab.
        - A thread puts it back on top every so often (without activating or moving it) while the widget exists."""
        try:
            hwnd = w.native.Handle.ToInt32()
        except Exception:
            return
        try:
            winmgr.tool_window_style(hwnd)
        except Exception as e:
            print("widget style:", e)
        winmgr.keep_topmost(hwnd, lambda: self._widget is w and not self._quitting)

    def _widget_saved_pos(self, w):
        """Where the widget was left: (x, y) in logical px for create_window, or None when there is nothing saved or
        that spot is no longer on any screen (monitor unplugged). The right edge is what is kept: the widget
        resizes with its top-right corner fixed."""
        pos = self.settings_get().get("widgetPos")
        try:
            right, top = float(pos["r"]), float(pos["y"])
        except Exception:
            return None
        k = winmgr.sys_scale()
        x, y = int(right - w), int(top)
        if not winmgr.on_screen(int((x + w / 2) * k), int((y + 12) * k)) or not winmgr.on_screen(int((x + w - 24) * k), int((y + 12) * k)):
            return None
        return x, y

    def _widget_track(self, w):
        """Saves the position (debounced) every time the widget is moved or resized; also the size is in
        widgetSize_<mode>, saved by widget_drag_resize."""
        try:
            from System import Action, EventHandler
            form = w.native

            def changed(sender, args):
                t = self._widget_pos_timer
                if t is not None:
                    t.cancel()
                t = self._widget_pos_timer = threading.Timer(0.6, lambda: self._widget_save_pos(w))
                t.daemon = True
                t.start()
            form.BeginInvoke(Action(lambda: (form.add_LocationChanged(EventHandler(changed)), form.add_SizeChanged(EventHandler(changed)))))
        except Exception as e:
            print("widget track:", e)

    def _widget_save_pos(self, w):
        if self._widget is not w:
            return
        try:
            form = w.native
            loc, size = form.Location, form.Size
            if loc.X <= -30000:
                return
            k = winmgr.sys_scale()
            self.settings_set({"widgetPos": {"r": round((loc.X + size.Width) / k, 1), "y": round(loc.Y / k, 1)}})
        except Exception as e:
            print("widget pos:", e)

    def _widget_gone(self, w=None):
        if w is not None and self._widget is not w:
            return  # it was an earlier widget that has already been replaced
        self._widget = None
        if w is not None:
            self._sender_drop(w)
        self._tray and self._tray.refresh()
        self._js("SH.emit('widget', {on: false})")

    def widget_close(self):
        """It closes a moment after replying: if the call comes from the widget itself, destroying it
        while pywebview is still returning the result to it can leave it hanging."""
        w = self._widget
        if w is not None:
            self._widget_save_pos(w)
        self._widget = None
        if w is not None:
            def kill():
                self._sender_drop(w)
                try:
                    w.destroy()
                except Exception:
                    pass
            threading.Timer(0.08, kill).start()
        self.settings_set({"widgetWasOpen": False}) if not self._quitting else None
        self._tele_refresh()
        self._tray and self._tray.refresh()
        self._js("SH.emit('widget', {on: false})")
        return True

    def widget_mode(self, mode):
        """The widget's own small/large button: saves it and tells the main window."""
        mode = "mini" if mode == "mini" else "full"
        self.settings_set({"widgetMode": mode})
        self._js(f"SH.state.settings.widgetMode='{mode}'; SH.emit('widget-mode','{mode}')")
        return mode

    def widget_is_open(self):
        return self._widget is not None

    def widget_update(self, conf):
        """Widget settings changed in the main window: applied instantly."""
        if self._widget is not None:
            self._js(f"window.SH && SH.widgetApply && SH.widgetApply({json.dumps(conf)})", self._widget)
        return True

    def widget_resize(self, width, height):
        """Resizes it keeping the top-right corner fixed (the widget usually lives on the right)."""
        if self._widget is not None:
            try:
                from webview.window import FixPoint
                self._widget.resize(int(width), int(height), fix_point=FixPoint.NORTH | FixPoint.EAST)
            except Exception as e:
                print("widget_resize:", e)
        return True

    def widget_limits(self, min_w, min_h, max_w, max_h):
        """Smallest and largest size the user can drag the widget to (device pixels), computed by the widget
        from what it shows. Windows enforces them during the resize loop."""
        w = self._widget
        if w is None:
            return False
        try:
            from System import Action
            from System.Drawing import Size
            form = w.native

            def apply():
                form.MinimumSize = Size(int(min_w), int(min_h))
                form.MaximumSize = Size(int(max_w), int(max_h))
            form.BeginInvoke(Action(apply))
            return True
        except Exception as e:
            print("widget_limits:", e)
            return False

    def widget_drag_resize(self, edge):
        """The user drags an edge of the widget: Windows' own resize loop (smooth, like any window).
        When it ends, the new size is saved for that widget size mode so it is kept next time."""
        w = self._widget
        hit = {"w": 10, "e": 11, "n": 12, "nw": 13, "ne": 14, "s": 15, "sw": 16, "se": 17}.get(edge)
        if w is None or not hit:
            return False
        try:
            from System import Action
            form = w.native
            u = ctypes.windll.user32

            def go():
                if not (u.GetAsyncKeyState(0x01) & 0x8000):
                    return  # the button was already released: do not start a loop stuck to the cursor
                u.ReleaseCapture()
                u.SendMessageW(form.Handle.ToInt32(), 0x00A1, hit, 0)  # WM_NCLBUTTONDOWN: blocks until released
                mode = self.settings_get().get("widgetMode") or "full"
                self.settings_set({f"widgetSize_{mode}": [w.width, w.height]})
                self._js("window.SH && SH.widgetResized && SH.widgetResized()", w)
            form.BeginInvoke(Action(go))
            return True
        except Exception as e:
            print("widget_drag_resize:", e)
            return False


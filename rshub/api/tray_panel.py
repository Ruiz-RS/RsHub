"""The tray panel (left click on the tray icon)."""
import json
import threading
import time
from pathlib import Path

from .. import config, winmgr


class TrayPanelApi:
    # ---------- tray panel (left click on the tray icon) ----------
    PANEL_W, PANEL_H = 344, 452  # CSS pixels

    def tray_panel_toggle(self):
        """Shows the panel above the tray icon, or hides it if it is showing."""
        if self._panel_shown:
            return self.panel_hide()
        if time.time() - self._panel_hidden_at < 0.35:
            return False  # the click that just took the focus away from it: leave it closed
        if self._panel is None:
            self._panel_create()
            return True
        if self._panel_ready:
            self._panel_place_and_show()
        return True

    def _panel_create(self):
        import webview
        self._panel = webview.create_window(
            "RsHub", url=str(Path(config.UI_DIR) / "tray.html"), js_api=self, width=self.PANEL_W, height=self.PANEL_H,
            frameless=True, easy_drag=False, on_top=True, resizable=False, hidden=True, focus=True, background_color=config.BG)
        pw = self._panel

        def loaded():
            if self._panel_ready:
                return
            self._panel_ready = True
            self._panel_setup(pw)
            self._panel_place_and_show()
        pw.events.loaded += loaded
        pw.events.closed += lambda: (setattr(self, "_panel", None), setattr(self, "_panel_ready", False), self._sender_drop(pw))

    def _panel_setup(self, pw):
        """Tool window (no taskbar button, no Alt+Tab), rounded corners, and hide as soon as it loses focus."""
        try:
            from System import Action, EventHandler
            form = pw.native
            winmgr.tool_window_style(form.Handle.ToInt32())
            winmgr.round_corners(pw)

            def on_deactivate(sender, args):
                if self._panel_shown:
                    self._panel_shown = False
                    self._panel_hidden_at = time.time()
                    form.Hide()
                    # off the window thread: it reads the settings and stops the GPU session, none of which it may wait for
                    threading.Thread(target=self._tele_refresh, daemon=True, name="tele-refresh").start()
            form.BeginInvoke(Action(lambda: form.add_Deactivate(EventHandler(on_deactivate))))
        except Exception as e:
            print("tray panel setup:", e)

    def _panel_place_and_show(self):
        """Above the tray (or next to it when the taskbar is on a side), inside the work area of the monitor
        under the cursor, in device pixels at the process (system) scaling."""
        pw = self._panel
        if pw is None:
            return
        try:
            from System import Action
            x, y, w, h = winmgr.cursor_monitor_geometry(self.PANEL_W, self.PANEL_H)
            form = pw.native
            tray_hwnd = getattr(getattr(self._tray, "icon", None), "_hwnd", None)

            def show():
                winmgr.show_topmost(form, x, y, w, h, tray_hwnd)
            self._panel_shown = True
            form.BeginInvoke(Action(show))
            self._tele_refresh()
            self._js("window.SH && SH.panelShown && SH.panelShown()", pw)
        except Exception as e:
            print("tray panel:", e)

    def panel_hide(self):
        pw = self._panel
        if pw is None or not self._panel_shown:
            return False
        self._panel_shown = False
        self._panel_hidden_at = time.time()
        self._tele_refresh()
        try:
            from System import Action
            form = pw.native
            form.BeginInvoke(Action(form.Hide))
        except Exception:
            pass
        return True

    def panel_go(self, mod=None, tab=None):
        """From the panel: open the main window (on a module, a tab or the settings)."""
        self.panel_hide()
        self.show_main()
        if mod == "settings":
            self._js("SH.go('settings')")
        elif mod:
            t = f", {{tab: {json.dumps(tab)}}}" if tab else ""
            self._js(f"SH.go({json.dumps(mod)}{t})")
        return True


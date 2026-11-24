"""Settings, language/look changes, Windows startup and administrator switches, import/export/reset."""
import json
import os
from pathlib import Path

from .. import autostart, config, elevation, settings
from ..i18n import tr


class SettingsApi:
    def settings_get(self):
        return settings.get()

    def settings_set(self, changes):
        if not isinstance(changes, dict):
            return False
        if not settings.update(changes):
            return False  # the file is there but can't be read right now: never overwrite it with less
        if "language" in changes:
            self._language_changed(changes["language"])
        if any(k in changes for k in self.LOOK_KEYS):
            self._look_changed()
        if "measurePing" in changes and self._sampler:
            self._sampler.ping_on = changes["measurePing"] is not False
        return True

    def _language_changed(self, pref):
        """New interface language: the engine and tray follow, and the widget reloads in the new language
        (the main window reloads itself)."""
        from .. import i18n
        i18n.set_language(pref)
        if self._tray is not None and hasattr(self._tray, "rebuild"):
            try:
                self._tray.rebuild()
            except Exception:
                pass
        reload = f"try{{localStorage.setItem('sh.lang', {json.dumps(pref or 'auto')})}}catch(e){{}};location.reload()"
        if self._widget is not None:
            self._js(reload, self._widget)
        if self._panel is not None:   # built once: it reloads (hidden) in the new language
            self._js(reload, self._panel)

    LOOK_KEYS = ("theme", "accent", "accent2", "intensity", "animSpeed")

    def _look_changed(self):
        """Theme, accent or animation changed: the widget and the tray panel re-read the settings and re-apply them."""
        for w in (self._widget, self._panel):
            if w is not None:
                self._js("window.SH && SH.lookChanged && SH.lookChanged()", w)

    def set_autostart(self, on):
        """Open with Windows (minimized to the tray): an entry in HKCU Run key."""
        return autostart.set_enabled(on)

    def set_always_admin(self, on):
        """With this on, RsHub always opens as administrator. An all-users install uses a scheduled task with
        highest privileges so Windows does not ask at every start (creating it does require admin); a per-user install,
        the portable copy and the source version ask Windows each time (elevation.uses_admin_task)."""
        self.settings_set({"alwaysAdmin": bool(on)})
        if config.PORTABLE or not elevation.uses_admin_task():
            # no task: this copy asks Windows each time it opens (elevation.elevate_on_start), as the setting says.
            # ensure_admin_task() refuses such copies, which showed "Could not set up starting as administrator."
            # although the setting works; and deleting the task here would remove the all-users install's, on a PC
            # that has both. An unsafe task an earlier version made goes now, if this copy is elevated.
            if config.is_admin() and not config.PORTABLE and not config.ISOLATED:
                elevation.remove_unsafe_admin_task()
            return True
        if on and config.is_admin():
            return elevation.ensure_admin_task()
        if not on:
            elevation.delete_admin_task()
        return True

    def open_data_dir(self):
        Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
        os.startfile(config.DATA_DIR)
        return True

    def export_settings(self):
        import webview
        r = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename="RsHub-settings.json",
                                            file_types=(tr("RsHub settings") + " (*.json)",))
        if not r:
            return None
        path = r if isinstance(r, str) else r[0]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.settings_get(), fh, ensure_ascii=False, indent=2)
        return path

    def import_settings(self):
        import webview
        r = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=(tr("RsHub settings") + " (*.json)",))
        if not r:
            return None
        try:
            with open(r[0], encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not settings.valid_import(data):
            return False
        self.settings_set(data)
        return data

    def reset_settings(self):
        """Puts the preferences back to their defaults and returns the old settings (so it can be undone). The user's
        data in the settings file is kept, and what lives outside it follows: no Windows startup entry and no
        always-as-administrator task once their switches are off."""
        old, ok = settings.reset()
        if not ok:
            return old
        if old.get("startWithWindows"):
            self.set_autostart(False)
        if old.get("alwaysAdmin") and elevation.uses_admin_task():  # (other copies have no task; the all-users one's stays)
            try:
                elevation.delete_admin_task()
            except Exception:
                pass
        return old

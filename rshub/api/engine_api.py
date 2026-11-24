"""Engine calls, jobs, telemetry glue, About data, Windows tools."""
import json
import os
import threading
from pathlib import Path

from .. import config, winmgr


class EngineApi:
    # ---------- real engine ----------
    def engine(self, mod, fn, args=None):
        """SH.engine(mod, fn, ...args): quick call into rshub/engine/<mod>.py (only what is in EXPORTS)."""
        from .. import engine as E
        try:
            return {"ok": True, "data": E.resolve(mod, fn, "call")(*(args or []))}
        except Exception as e:
            import traceback
            print(f"[engine {mod}.{fn}]", traceback.format_exc())
            return {"ok": False, "error": str(e)}

    def engine_job(self, mod, fn, args=None):
        """SH.job(...): long task; progress arrives as SH.emit('job', ...). Returns the task id."""
        from .. import engine as E
        from ..engine import jobs
        try:
            return {"ok": True, "id": jobs.start(f"{mod}.{fn}", E.resolve(mod, fn, "job"), *(args or []))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def jobs_running(self):
        """Tasks still running in Python: after the page reloads (language change) the UI asks for them."""
        from ..engine import jobs
        return jobs.running()

    def job_cancel(self, job_id):
        from ..engine import jobs
        return jobs.cancel(job_id)

    def pc_info(self):
        from ..engine import system
        return system.pc_info()

    def screen_thumb(self, index=0, width=480):
        from ..engine import system
        try:
            return system.screen_thumb(index, width)
        except Exception:
            return None

    def tele_rate(self, ms, lite=False):
        """The UI sends the refresh period it wants (already slower on battery) and whether it is in its
        lighter mode, so the sampler also skips the slow extras (latency every 6 s, Wi-Fi every minute)."""
        self._pending_rate = (ms, lite)  # kept for a sampler that is still being created at start
        if self._sampler:
            self._sampler.lite = bool(lite)
            self._sampler.set_rate(float(ms) / 1000)
        return True

    def _tele_push(self, data):
        """Each telemetry sample goes to the main window (if visible) and to the widget (if open).
        Called from the sampler thread, never from the window thread."""
        payload = json.dumps(data)
        code = f"window.SH && SH.tele.push && SH.tele.push({payload})"
        targets = []
        if self._visible and not self._minimized:
            targets.append(self._window)
        if self._widget is not None:
            targets.append(self._widget)
        if self._panel is not None and self._panel_shown:
            targets.append(self._panel)
        for w in targets:
            self._tele_runner(w).run(code)

    def _tele_runner(self, w):
        """One Runner per window: a page that never answers costs this sample (5 s at most), not the live data."""
        from ..jssender import Runner
        rs = self.__dict__.setdefault("_tele_runners", {})
        r = rs.get(w)
        if r is None:
            r = rs[w] = Runner(w, "tele-js", timeout=5.0)
        return r

    def _tele_refresh(self):
        shown = self._visible and not self._minimized
        winmgr.wv_visible(self._window, shown, trim=not self._visible)  # (minimized: back soon, not trimmed)
        if getattr(self, "_told_shown", None) != shown:
            # the page cannot rely on document.hidden for this (WebView2 may keep it "visible" while the window is
            # hidden or minimized): SH.activity uses it to tell the end of long work in a Windows notification instead
            self._told_shown = shown
            self._js(f"window.SH && SH.emit && SH.emit('window', {{shown: {'true' if shown else 'false'}}})")
        winmgr.wv_visible(self._panel, self._panel_shown)
        winmgr.wv_visible(self._widget, True)   # (re)assert: a widget WebView2 only ever renders while it exists
        if self._sampler:
            stats = self.settings_get().get("widgetStats") or []
            gpu_in_widget = any(s.startswith("gpu") or s == "vram" for s in stats)
            self._sampler.set_active(shown or self._widget is not None or self._panel_shown,
                                     gpu=shown or self._panel_shown or (self._widget is not None and gpu_in_widget))

    # ---------- about ----------
    def project_stats(self):
        """Bytes of code per language, like GitHub's language bar."""
        if config.FROZEN:  # the installed app has no .py files: the build saved the numbers
            try:
                with open(Path(config.base_dir()) / "rshub" / "project_stats.json", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
        langs = {".py": "Python", ".js": "JavaScript", ".css": "CSS", ".html": "HTML"}
        out = {}
        root = Path(config.base_dir()) / "rshub"
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ("vendor", "fonts", "__pycache__")]
            for f in files:
                lang = langs.get(os.path.splitext(f)[1].lower())
                if lang:
                    try:
                        out[lang] = out.get(lang, 0) + (Path(dirpath) / f).stat().st_size
                    except OSError:
                        pass
        return out

    # ---------- Windows tools and data ----------
    TOOLS = {
        "devmgmt": "devmgmt.msc", "services": "services.msc", "eventvwr": "eventvwr.msc", "taskschd": "taskschd.msc",
        "diskmgmt": "diskmgmt.msc", "regedit": "regedit.exe", "msconfig": "msconfig.exe", "msinfo32": "msinfo32.exe",
        "resmon": "resmon.exe", "control": "control.exe", "powercfg": "powercfg.cpl", "sysdm": "sysdm.cpl",
        "ncpa": "ncpa.cpl", "appwiz": "appwiz.cpl", "perf": "SystemPropertiesPerformance.exe", "cleanmgr": "cleanmgr.exe",
        "dfrgui": "dfrgui.exe", "rstrui": "rstrui.exe", "taskmgr": "taskmgr.exe", "cmd": "cmd.exe", "powershell": "powershell.exe",
    }

    def run_tool(self, name):
        exe = self.TOOLS.get(name)
        if not exe:
            return False
        try:
            os.startfile(exe)
            return True
        except OSError:
            return False
    def open_url(self, url):
        if isinstance(url, str) and url.startswith(("https://", "http://", "ms-settings:")):
            os.startfile(url)
            return True
        return False


def attach_telemetry(api, window):
    """Builds the telemetry sampler off the start path (opening the performance counters takes ~0.5 s) and starts it
    when the page has loaded."""
    def build_sampler():
        try:
            from ..engine.telemetry import Sampler
            sampler = Sampler(api._tele_push)
            sampler.set_rate(float(api.settings_get().get("teleRate") or 1000) / 1000)
            sampler.ping_on = api.settings_get().get("measurePing", True) is not False
            pend = getattr(api, "_pending_rate", None)
            if pend:
                sampler.lite = bool(pend[1])
                sampler.set_rate(float(pend[0]) / 1000)
            api._sampler = sampler
        except Exception as e:
            print("telemetry:", e)
    sampler_thread = threading.Thread(target=build_sampler, daemon=True, name="sampler-init")
    sampler_thread.start()

    def start_sampler():
        sampler_thread.join(10)
        if api._sampler:
            api._sampler.start()
            api._tele_refresh()
    window.events.loaded += lambda: threading.Thread(target=start_sampler, daemon=True).start()

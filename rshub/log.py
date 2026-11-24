"""The start timeline ([phase] lines), the log file used when there is no console, and the last-resort error box."""
import ctypes
import os
import sys
import time
from pathlib import Path

from . import config, paths


def since_start():
    """Seconds since this process was created (Windows' own creation time: includes the interpreter/exe start)."""
    try:
        from ctypes import wintypes as wt
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        c, e, kt, ut = (wt.FILETIME() for _ in range(4))
        k32.GetProcessTimes(ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
        created = ((c.dwHighDateTime << 32) | c.dwLowDateTime) / 1e7 - 11644473600
        return max(0.0, time.time() - created)
    except Exception:
        return 0.0


_PHASES_SEEN = set()


def phase(name, once=True):
    """One log line per start phase, with the time since the process was created: [phase] 1.23 s name.
    That is how a slow start is told apart from a slow piece of it."""
    if once and name in _PHASES_SEEN:
        return
    _PHASES_SEEN.add(name)
    try:
        print(f"[phase] {since_start():6.2f} s  {name}", flush=True)
    except Exception:
        pass


class LogOnce:
    """Wraps the log file: the same line repeated back to back (a failing sampler, a UI error every second) is
    written once and then counted, so the log stays readable and small."""

    def __init__(self, fh):
        import threading
        self.fh, self.last, self.n, self.buf = fh, None, 0, ""
        self._lock = threading.RLock()  # every thread prints: without it, lines from two threads could be lost or mixed

    def write(self, text):
        with self._lock:
            return self._write(text)

    def _write(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            key = line.strip()
            if key and key == self.last:
                self.n += 1
                continue
            self._flush_repeats()
            self.last = key or None
            self.fh.write(line + "\n")
        return len(text)

    def _flush_repeats(self):
        if self.n:
            self.fh.write(f"  (the same line {self.n} more time{'s' if self.n > 1 else ''})\n")
            self.n = 0

    def flush(self):
        try:
            with self._lock:
                self._flush_repeats()
                self.fh.flush()
        except (OSError, ValueError):
            pass

    def isatty(self):
        return False


def rotate_log(path, limit=2 * 1024 * 1024):
    """Above 2 MB the log becomes rshub.log.1 (one older copy is kept) and a new one starts."""
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except OSError:
        pass


def log_to_file():
    """With pythonw there is no console: errors go to the rshub.log in the data folder."""
    if sys.stderr is None or "pythonw" in os.path.basename(sys.executable).lower():
        try:
            Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
            path = str(Path(config.DATA_DIR) / "rshub.log")
            rotate_log(path)
            sys.stdout = sys.stderr = LogOnce(open(path, "a", encoding="utf-8", buffering=1))
        except OSError:
            pass


def fatal(msg):
    """A last resort that never stays silent: the log and a Windows message box."""
    print("[start]", msg, flush=True)
    try:
        from . import splash
        splash.close()
    except Exception:
        pass
    if not paths.dialogs_allowed():
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, "RsHub", 0x10 | 0x00040000)  # error icon, topmost
    except Exception:
        pass

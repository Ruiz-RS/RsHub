"""Constants and locations shared by the whole app: where the UI, the data and the settings file are, which mode this
copy runs in (installed, portable, isolated test copy) and how to launch it again. No heavy imports: this is loaded
first, on the start path."""
import ctypes
import os
import sys
import tempfile
from pathlib import Path

from . import paths

APP_ID = "RsHub.App"
BG = "#000000"
FROZEN = bool(getattr(sys, "frozen", False))  # running as the installed RsHub.exe
PORTABLE = paths.PORTABLE                     # the portable version keeps everything in a Data folder next to the program
# RSHUB_ISOLATED=1: a test copy that runs next to the user's (own lock, ports and browser profile)
ISOLATED = os.environ.get("RSHUB_ISOLATED") == "1"


def base_dir():
    """The folder the program runs from: the PyInstaller bundle, or the project folder from source."""
    return getattr(sys, "_MEIPASS", str(Path(os.path.abspath(__file__)).parent.parent))


UI_DIR = str(Path(base_dir()) / "rshub" / "ui")
DATA_DIR = paths.data_dir()
SETTINGS = str(Path(DATA_DIR) / "settings.json")
WEB_DIR = str(Path(paths.local_dir()) / "webview")
if ISOLATED:
    WEB_DIR = str(Path(tempfile.gettempdir()) / f"rshub_isolated_{os.getpid()}")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def gui_python():
    """pythonw.exe if it exists, so relaunching does not open a console."""
    exe = sys.executable
    if not FROZEN and exe.lower().endswith("python.exe"):
        w = exe[:-10] + "pythonw.exe"
        if os.path.exists(w):
            return w
    return exe


def launch_cmd(extra=""):
    """(program, arguments) that start this copy again: the exe, or pythonw + run.py from source."""
    if FROZEN:
        return sys.executable, extra.strip()
    return gui_python(), f'"{Path(base_dir()) / "run.py"}" {extra}'.strip()

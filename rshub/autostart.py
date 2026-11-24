"""The "open with Windows" entry (HKCU Run key), kept pointing at this copy."""
import os
import sys

from . import config, settings

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "RsHub"


def command():
    exe, args = config.launch_cmd("--tray")
    return f'"{exe}" {args}'


def write(on):
    """Adds or removes the entry. OSError when the registry refuses."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass


def set_enabled(on):
    """Open with Windows (minimized to the tray). The portable version never adds itself: a USB drive is not
    always there."""
    if config.PORTABLE and on:
        return False
    try:
        write(on)
        return True
    except OSError:
        return False


def sync():
    """After installing (or moving) RsHub, the "open with Windows" entry must open this copy."""
    if config.PORTABLE or not settings.read().get("startWithWindows"):
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            try:
                cur = winreg.QueryValueEx(k, VALUE)[0]
            except FileNotFoundError:
                cur = None
            if cur != command():
                winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
    except OSError:
        pass


def remove_if_ours():
    """The uninstaller: drops the entry when it points at this copy."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            cur = winreg.QueryValueEx(k, VALUE)[0]
            if os.path.normcase(sys.executable) in os.path.normcase(cur):
                winreg.DeleteValue(k, VALUE)
    except OSError:
        pass

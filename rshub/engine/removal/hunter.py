"""Hunter mode: from a window, an .exe or a shortcut to the installed program."""
import ctypes
import os
from ctypes import wintypes

from . import shortcuts, system
from .paths import is_within, norm_name, norm_path

_u32 = ctypes.windll.user32
_u32.WindowFromPoint.argtypes = [wintypes.POINT]
_u32.WindowFromPoint.restype = wintypes.HWND
_u32.GetAncestor.restype = wintypes.HWND


def window_at_cursor():
    pt = wintypes.POINT()
    _u32.GetCursorPos(ctypes.byref(pt))
    hwnd = _u32.WindowFromPoint(pt)
    if not hwnd:
        return None
    root = _u32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    pid = wintypes.DWORD()
    _u32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    n = _u32.GetWindowTextLengthW(root)
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(root, buf, n + 1)
    return {"pid": pid.value, "title": buf.value, "path": system._proc_path(pid.value)}


def resolve_target(path):
    """If it is a shortcut, return its target."""
    if path and path.lower().endswith((".lnk", ".url")):
        t = shortcuts.read_shortcut(path).get("target")
        return t or path
    return path


def find_program(path, programs):
    """Program a file belongs to: the most specific install folder that contains it."""
    path = resolve_target(path)
    if not path:
        return None
    best, best_len = None, -1
    for p in programs:
        for cand in (p.get("location"),):
            if cand and is_within(path, cand) and len(norm_path(cand)) > best_len:
                best, best_len = p, len(norm_path(cand))
    if best:
        return best
    # no registered folder: match by the uninstaller's or icon's folder, or by the .exe name
    d = norm_path(os.path.dirname(path))
    for p in programs:
        for f in (p.get("icon", "").split(",")[0], p.get("uninstall", "")):
            f = f.strip().strip('"')
            if f and os.path.isabs(f.split('"')[0]) and norm_path(os.path.dirname(f.split('"')[0])) == d:
                return p
    stem = norm_name(os.path.splitext(os.path.basename(path))[0])
    if len(stem) >= 4:
        for p in programs:
            if norm_name(p.get("name")).startswith(stem) or stem.startswith(norm_name(p.get("name"))[:8] or "~"):
                return p
    return None

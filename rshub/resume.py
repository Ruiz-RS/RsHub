"""Resume tickets: coming back to the same place (and the same action) after the administrator relaunch.

A copy in normal mode that needs administrator rights for something the user just asked for (SH.requireAdmin in the
UI, see js/admin-resume.js) writes a small ticket before asking Windows for an elevated copy: where the user was
(module, tab, settings section, Home view, scroll positions, the text in its search fields) and, for the actions
that are registered for it, which action and on what (the drivers selected, the device...). The elevated copy reads
it once, deletes it and puts the user back there; the UI then runs the action again through its normal path, with its
normal confirmation, after checking the things it acts on still exist.

The ticket lives in the user's data folder, which any program running as the user can write. So:
  - the elevated copy only reads the ticket whose SHA-256 the normal copy passed on its command line (--resume <hash>,
    in the arguments of the UAC request, which a user-level program can't change); any other file is deleted unread;
  - it is valid for MAX_AGE seconds and read once (deleted through the same handle it was read from, after checking
    the handle is the plain file it should be: no link, no junction in the way);
  - its shape is checked field by field (validate): plain ids, short strings and numbers only, and an action must be
    in ACTIONS, the same list the UI registers (SH.resume.register). The UI never runs code or engine calls from it:
    each action is a function of the module that rebuilds the click from the module's current state."""
import ctypes
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import config

MAX_AGE = 120        # seconds a ticket is valid
MAX_BYTES = 32768
NAME = "resume.json"

# the actions the UI can run again after the relaunch (js/admin-resume.js and the modules' SH.resume.register calls;
# tests/test_admin_resume.py checks both lists match)
ACTIONS = frozenset({
    "drivers.install", "drivers.rollback", "drivers.toggle", "drivers.enable", "drivers.removeDevice",
    "drivers.removeAll", "drivers.backup", "drivers.restoreBackup", "drivers.storeRemove",
    "monitor.dns",
    "backups.protection", "backups.deletePoint", "backups.undo",
    "optimizer.startup",
    "storage.scan",
})

_MOD = re.compile(r"^[a-z]{2,24}$")
_TAB = re.compile(r"^[a-z]{1,24}$")
_VAR = re.compile(r"^[a-z0-9]{1,24}$")
_SEL = re.compile(r"^(?:#[A-Za-z][\w-]{0,60}|view|tab|settings)$")
_ACT = re.compile(r"^[a-z]{2,24}\.[a-z][A-Za-z]{1,40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def path():
    return str(Path(config.DATA_DIR) / NAME)


def _clean(v, depth=0):
    """A JSON value kept only if it is small and plain: strings up to 400 characters, finite numbers, booleans, None,
    lists up to 300 items and objects up to 40 keys (keys are short identifiers), at most 4 levels deep."""
    if depth > 4:
        raise ValueError("too deep")
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v or abs(v) > 1e15:
            raise ValueError("number")
        return v
    if isinstance(v, str):
        if len(v) > 400:
            raise ValueError("string")
        return v
    if isinstance(v, list):
        if len(v) > 300:
            raise ValueError("list")
        return [_clean(x, depth + 1) for x in v]
    if isinstance(v, dict):
        if len(v) > 40:
            raise ValueError("object")
        out = {}
        for k, x in v.items():
            if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,30}", k):
                raise ValueError("key")
            out[k] = _clean(x, depth + 1)
        return out
    raise ValueError("type")


def validate(t, now=None):
    """The ticket with only the fields it may have, each checked, or None if anything is off (wrong shape, expired,
    an action that is not in ACTIONS)."""
    if not isinstance(t, dict) or t.get("v") != 1:
        return None
    now = time.time() if now is None else now
    at = t.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool) or not (now - MAX_AGE <= at <= now + 5):
        return None
    out = {"v": 1, "at": at}
    try:
        for key, rx in (("module", _MOD), ("tab", _TAB), ("settings", _MOD), ("home", _VAR)):
            v = t.get(key, "")
            if v in ("", None):
                out[key] = ""
            elif isinstance(v, str) and rx.fullmatch(v):
                out[key] = v
            else:
                return None
        scroll = t.get("scroll", [])
        if not isinstance(scroll, list) or len(scroll) > 16:
            return None
        out["scroll"] = []
        for s in scroll:
            if not (isinstance(s, dict) and isinstance(s.get("k"), str) and _SEL.fullmatch(s["k"])
                    and isinstance(s.get("y"), (int, float)) and not isinstance(s.get("y"), bool) and 0 <= s["y"] <= 1e6):
                return None
            out["scroll"].append({"k": s["k"], "y": int(s["y"])})
        fields = t.get("fields", [])
        if not isinstance(fields, list) or len(fields) > 8:
            return None
        out["fields"] = []
        for f in fields:
            if not (isinstance(f, dict) and isinstance(f.get("k"), str) and f["k"].startswith("#") and _SEL.fullmatch(f["k"])
                    and isinstance(f.get("v"), str) and len(f["v"]) <= 200):
                return None
            out["fields"].append({"k": f["k"], "v": f["v"]})
        act = t.get("action", "")
        if act in ("", None):
            out["action"], out["args"] = "", {}
        elif isinstance(act, str) and _ACT.fullmatch(act) and act in ACTIONS:
            args = t.get("args", {})
            if not isinstance(args, dict):
                return None
            out["action"], out["args"] = act, _clean(args)
        else:
            return None
    except (ValueError, RecursionError):
        return None
    return out


def save(ticket):
    """The normal copy, right before asking Windows: writes the checked ticket (stamped now) and returns the SHA-256 of
    the bytes written, for the elevated copy's command line. None if the ticket is not valid or can't be written."""
    if isinstance(ticket, dict):
        ticket = dict(ticket, v=1, at=time.time())
    t = validate(ticket)
    if t is None:
        return None
    data = json.dumps(t, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_BYTES:
        return None
    p = path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + f".{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except OSError as e:
        print("resume ticket:", e, flush=True)
        return None
    return hashlib.sha256(data).hexdigest()


def discard():
    """Windows said no: the ticket goes (the normal copy, which wrote it)."""
    try:
        os.remove(path())
    except OSError:
        pass


def _long_path(p):
    """p absolute, with any 8.3 short names (RUNNER~1) in their long form. Links and junctions are NOT followed: a path
    through one keeps its own spelling, so it never matches the handle's final path."""
    from ctypes import wintypes
    p = os.path.abspath(p)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetLongPathNameW.restype = wintypes.DWORD
    k32.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    buf = ctypes.create_unicode_buffer(1024)
    n = k32.GetLongPathNameW(p, buf, 1024)
    return buf.value if 0 < n < 1024 else p


def _read_and_delete(p):
    """The file's bytes, read and then deleted through one handle that is checked to be the plain file at `p` (not a
    link, a junction on the way, a folder or a file with other names), so an elevated copy never deletes or reads
    something else. None if there is no such file or anything is off (it is then left alone)."""
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    GENERIC_READ, DELETE = 0x80000000, 0x00010000
    OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT = 3, 0x00200000
    h = k32.CreateFileW(p, GENERIC_READ | DELETE, 0, None, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, None)
    if not h or h == wintypes.HANDLE(-1).value:
        return None
    try:
        class INFO(ctypes.Structure):
            _fields_ = [("attrs", wintypes.DWORD), ("c", wintypes.FILETIME), ("a", wintypes.FILETIME),
                        ("w", wintypes.FILETIME), ("serial", wintypes.DWORD), ("sizeHigh", wintypes.DWORD),
                        ("sizeLow", wintypes.DWORD), ("links", wintypes.DWORD), ("idHigh", wintypes.DWORD),
                        ("idLow", wintypes.DWORD)]
        info = INFO()
        if not k32.GetFileInformationByHandle(h, ctypes.byref(info)):
            return None
        if info.attrs & (0x10 | 0x400) or info.links != 1 or info.sizeHigh or info.sizeLow > MAX_BYTES:
            return None  # a folder, a reparse point (link), a hard link to something else, or far too big
        buf = ctypes.create_unicode_buffer(1024)
        n = k32.GetFinalPathNameByHandleW(h, buf, 1024, 0)
        final = buf.value[:n] if 0 < n < 1024 else ""
        final = final[4:] if final.startswith("\\\\?\\") else final
        # the path Windows resolved (junctions and links followed, long names) against the path asked for, with its
        # 8.3 short names spelled out but nothing followed (GetLongPathNameW): C:\Users\RUNNER~1\... is the same file
        if not final or os.path.normcase(final) != os.path.normcase(_long_path(p)):
            return None  # a junction or link somewhere on the way: the file is not where it says
        data = ctypes.create_string_buffer(info.sizeLow or 1)
        got = wintypes.DWORD(0)
        if info.sizeLow and not k32.ReadFile(h, data, info.sizeLow, ctypes.byref(got), None):
            return None
        raw = data.raw[:got.value]

        class DISPOSITION(ctypes.Structure):
            _fields_ = [("delete", wintypes.BOOLEAN)]
        d = DISPOSITION(True)
        k32.SetFileInformationByHandle(h, 4, ctypes.byref(d), ctypes.sizeof(d))  # FileDispositionInfo
        return raw
    finally:
        k32.CloseHandle(h)


_taken = False


def take(token, now=None):
    """The elevated copy, once: the ticket whose hash came on its command line, or None. The file is deleted whatever
    it holds; a second call (the page reloaded for a language change) gets nothing."""
    global _taken
    if _taken:
        return None
    _taken = True
    p = path()
    raw = _read_and_delete(p)
    if raw is None:
        return None
    if not (isinstance(token, str) and _HASH.fullmatch(token)) or hashlib.sha256(raw).hexdigest() != token:
        print("resume ticket: it is not the one this copy was started for; ignored", flush=True)
        return None
    try:
        t = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return validate(t, now)


def token_from_argv(argv):
    """The ticket hash on the command line (--resume <hash>), or None."""
    if "--resume" in argv:
        i = argv.index("--resume")
        if i + 1 < len(argv) and _HASH.fullmatch(argv[i + 1]):
            return argv[i + 1]
    return None

"""Program icons as PNG (data URI), with an on-disk cache.

Cold extraction takes ~200 ms per program (antivirus + disk); warm, < 1 ms.
"""
import base64
import ctypes
import hashlib
import os
import re
import threading
from ctypes import wintypes

from . import paths
from .programs import exe_from_command, msi_info

_lock = threading.Lock()
_net = None


def _dotnet():
    global _net
    if _net is None:
        try:
            import clr  # pythonnet (ships with pywebview)
            clr.AddReference("System.Drawing")
            from System import IntPtr
            from System.Drawing import Icon
            from System.Drawing.Imaging import ImageFormat
            from System.IO import MemoryStream
            _net = (IntPtr, Icon, ImageFormat, MemoryStream)
        except Exception:
            _net = False
    return _net


_shell32 = ctypes.windll.shell32
_user32 = ctypes.windll.user32
_shell32.SHDefExtractIconW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.UINT,
                                       ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT]


def _hicon_bytes(h):
    """PNG bytes of an HICON (the caller destroys it)."""
    net = _dotnet()
    if not net or not h:
        return None
    IntPtr, Icon, ImageFormat, MemoryStream = net
    try:
        with _lock:
            bmp = Icon.FromHandle(IntPtr(h)).ToBitmap()
            ms = MemoryStream()
            bmp.Save(ms, ImageFormat.Png)
            data = bytes(ms.ToArray())
            bmp.Dispose()
        return data
    except Exception:
        return None


def _hicon_png(path, idx, size=48):
    if not _dotnet():
        return None
    h = wintypes.HICON()
    try:
        hr = _shell32.SHDefExtractIconW(path, idx, 0, ctypes.byref(h), None, (size & 0xFFFF) | (size << 16))
    except Exception:
        return None
    if hr != 0 or not h.value:
        return None
    try:
        return _hicon_bytes(h.value)
    finally:
        _user32.DestroyIcon(h)


def parse_icon_spec(spec):
    spec = os.path.expandvars((spec or "").strip())
    m = re.match(r'^"?(.*?)"?(?:,\s*(-?\d+))?$', spec)
    if not m:
        return None, 0
    return m.group(1).strip('" '), int(m.group(2) or 0)


def _exes_in(loc, name):
    """The program's own .exe files in a folder (not uninstallers or updaters), most likely first."""
    try:
        exes = [f for f in os.listdir(loc) if f.lower().endswith(".exe")
                and not re.match(r"(unins|uninst|setup|update|crash|helper)", f, re.I)]
    except OSError:
        exes = []
    key = paths.norm_name(name or "")[:6]
    exes.sort(key=lambda f: (key not in paths.norm_name(f), -_safe_size(os.path.join(loc, f))))
    return [os.path.join(loc, f) for f in exes[:2]]


_pf_subdirs = {}


def _guess_dirs(name):
    """Folders where a program with no location in the registry usually lives: Program Files\\<name> or
    Program Files\\<maker>\\<name> (MSI products such as "GitHub CLI" or "ROG Live Service" say nothing else)."""
    base = re.sub(r"\s+(x64|x86|\(x64\)|\(x86\)|v?\d+(\.\d+)*)$", "", (name or "").strip(), flags=re.I)
    names = [n for n in dict.fromkeys([(name or "").strip(), base]) if n and not re.search(r'[\\/:*?"<>|]', n)]
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    for root in filter(None, roots):
        if root not in _pf_subdirs:
            try:
                _pf_subdirs[root] = [os.path.join(root, d) for d in os.listdir(root)]
            except OSError:
                _pf_subdirs[root] = []
        for n in names:
            for d in [os.path.join(root, n)] + [os.path.join(s, n) for s in _pf_subdirs[root]]:
                if os.path.isdir(d):
                    yield d


def _store_app_id(loc):
    """Id of the first <Application> in the package manifest (for shell:AppsFolder\\<family>!<id>)."""
    try:
        with open(os.path.join(loc, "AppxManifest.xml"), "rb") as f:
            m = re.search(rb'<Application\s[^>]*?\bId="([^"]+)"', f.read())
        return m.group(1).decode() if m else None
    except OSError:
        return None


def _candidates(p):
    """Possible icon sources, in order of preference. idx None = plain PNG, "aumid" = Start menu icon of a
    Store app (sparse packages keep their logos outside the package folder)."""
    if p.get("source") == "store":
        if p.get("icon"):
            yield p["icon"], None
        if p.get("family") and p.get("location"):
            app = _store_app_id(p["location"])
            if app:
                yield f"{p['family']}!{app}", "aumid"
        return
    path, idx = parse_icon_spec(p.get("icon"))
    if path:
        yield path, idx
    if p.get("msi_code"):
        mi = msi_info(p["msi_code"], "ProductIcon")
        if mi:
            yield mi, 0
    loc = p.get("location")
    if loc and os.path.isdir(loc):
        for f in _exes_in(loc, p.get("name")):
            yield f, 0
    u = exe_from_command(p.get("uninstall"))
    if u:
        yield u, 0
    if p.get("source") == "file" or not p.get("name"):
        return
    # nothing in the registry: look where the installer most likely put it
    msi_loc = msi_info(p["msi_code"], "InstallLocation") if p.get("msi_code") else None
    dirs = ([msi_loc] if msi_loc and os.path.isdir(msi_loc) else []) + ([] if loc else list(_guess_dirs(p.get("name")))[:2])
    for d in dirs:
        for f in _exes_in(d, p.get("name")) or _exes_in(os.path.join(d, "bin"), p.get("name")):
            yield f, 0


def _safe_size(f):
    try:
        return os.path.getsize(f)
    except OSError:
        return 0


def _uri(data):
    return "data:image/png;base64," + base64.b64encode(data).decode() if data else None


def get_icon(p, extract=True):
    """PNG data URI of the icon, or None.

    extract=False: only looks at the disk cache (microseconds) and returns None if the first candidate
    has not been extracted yet, so the result matches what the full extraction will return later.
    A program whose .exe files carry no icon (console tools such as gh.exe) gets the generic program icon,
    like in Explorer.
    """
    had_exe = False
    for path, idx in _candidates(p):
        if idx == "aumid":
            key = hashlib.sha1(f"aumid|{path}|{p.get('version', '')}".encode()).hexdigest()
        else:
            if not path or not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = hashlib.sha1(f"{paths.norm_path(path)}|{idx}|{st.st_mtime}|{st.st_size}".encode()).hexdigest()
            had_exe = had_exe or path.lower().endswith(".exe")
        cache = paths.data_path("cache", "icons", f"{key}.png", make=False)
        try:
            with open(cache, "rb") as f:
                data = f.read()
        except OSError:
            data = None
        if data:
            return _uri(data)
        if data is not None:
            continue  # negative cache: this candidate has no icon
        if not extract:
            return None
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        if idx == "aumid":  # what the Start menu shows for the app
            data = _parsed_png(f"shell:AppsFolder\\{path}")
            if _looks_blank(data):
                data = None
        elif idx is None:  # plain PNG (Store)
            with open(path, "rb") as f:
                data = f.read()
        else:  # .exe, .dll or .ico
            data = _hicon_png(path, idx or 0)
        try:
            with open(cache, "wb") as f:
                f.write(data or b"")
        except OSError:
            pass
        if data:
            return _uri(data)
    return type_icon(".exe") if had_exe and p.get("source") != "file" else None


def icon_for_file(path):
    return get_icon({"source": "file", "icon": path})


# ---------------------------------------------------------------- what Explorer shows: file types, Windows, stock icons
# Shared by every module (processes, services, files): small in-memory cache, one entry per type or name.
class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260), ("szTypeName", wintypes.WCHAR * 80)]


class _SHSTOCKICONINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hIcon", wintypes.HICON), ("iSysImageIndex", ctypes.c_int),
                ("iIcon", ctypes.c_int), ("szPath", wintypes.WCHAR * 260)]


class _GUID(ctypes.Structure):
    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]


_IID_IImageList = _GUID(0x46EB5926, 0x582E, 0x4017, (ctypes.c_ubyte * 8)(0x9F, 0xDF, 0xE8, 0x99, 0x8D, 0xAA, 0x09, 0x50))
_ole32 = ctypes.windll.ole32
_comctl = ctypes.windll.comctl32
_shell32.SHGetFileInfoW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_SHFILEINFOW), wintypes.UINT, wintypes.UINT]
_shell32.SHGetFileInfoW.restype = ctypes.c_size_t
_shell32.SHGetImageList.argtypes = [ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHParseDisplayName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), wintypes.ULONG,
                                        ctypes.POINTER(wintypes.ULONG)]
_shell32.SHGetStockIconInfo.argtypes = [ctypes.c_int, wintypes.UINT, ctypes.POINTER(_SHSTOCKICONINFO)]
_comctl.ImageList_GetIcon.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.UINT]
_comctl.ImageList_GetIcon.restype = wintypes.HICON
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_sys_list = None
_mem = {}
# SHSTOCKICONID values (shellapi.h)
STOCK = {"application": 2, "folder": 3, "drive": 8, "cd": 11, "world": 13, "server": 15, "printer": 16, "network": 17,
         "recycler": 31, "lock": 47, "shield": 77, "key": 81, "software": 82, "desktop": 94, "laptop": 95, "users": 96,
         "phone": 99, "camera": 100, "videocamera": 101, "audio": 102, "internet": 104, "zip": 105, "settings": 106}


def _com():
    try:
        _ole32.CoInitializeEx(None, 2)  # the shell needs COM on this thread; harmless if already done
    except Exception:
        pass


def _index_png(i):
    """Icon number i of the system image list at 48 px (the "extra large" list)."""
    global _sys_list
    if _sys_list is None:
        try:
            _shell32[660](1)  # FileIconInit(TRUE): without it the first lists come back with blank icons
        except Exception:
            pass
        h = ctypes.c_void_p()
        _sys_list = h.value if _shell32.SHGetImageList(2, ctypes.byref(_IID_IImageList), ctypes.byref(h)) == 0 else 0
    if not _sys_list or i < 0:
        return None
    hicon = _comctl.ImageList_GetIcon(_sys_list, i, 1)  # ILD_TRANSPARENT
    if not hicon:
        return None
    try:
        return _hicon_bytes(hicon)
    finally:
        _user32.DestroyIcon(hicon)


def _shell_png(name, attrs=0x80, flags=0x10):
    """Icon Explorer shows for a name (SHGFI_USEFILEATTRIBUTES: the file does not need to exist) or a PIDL."""
    _com()
    info = _SHFILEINFOW()
    if not _shell32.SHGetFileInfoW(name, attrs, ctypes.byref(info), ctypes.sizeof(info), 0x4000 | flags):  # SYSICONINDEX
        return None
    return _index_png(info.iIcon)


def _parsed_png(parsing_name):
    """Icon of a shell item given by its parsing name, e.g. shell:AppsFolder\\<family>!<app>."""
    _com()
    pidl = ctypes.c_void_p()
    if _shell32.SHParseDisplayName(parsing_name, None, ctypes.byref(pidl), 0, None) != 0 or not pidl.value:
        return None
    try:
        return _shell_png(pidl, 0, 0x8)  # SHGFI_PIDL
    finally:
        _ole32.CoTaskMemFree(pidl)


def _mem_get(key, make):
    if key not in _mem:
        try:
            _mem[key] = make()
        except Exception:
            _mem[key] = None
    return _mem[key]


def _looks_blank(data):
    """The shell gives a blank page (no icon) or a solid square (broken asset) for some packages."""
    if not data:
        return True
    if data == _mem_get(("type", ".zzunknown"), lambda: _shell_png("x.zzunknown")):
        return True
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        lo, hi = zip(*im.getextrema())
        return lo[3] == 255 and all(h - lw < 12 for lw, h in zip(lo[:3], hi[:3]))  # opaque and one flat color
    except Exception:
        return False


def type_icon(name_or_ext, folder=False):
    """Data URI of the icon Explorer shows for a file type ('.mp4', 'clip.mp4') or a folder. Cached per type."""
    if folder:
        return _uri(_mem_get(("type", "<dir>"), lambda: _shell_png("folder", 0x10)))
    s = str(name_or_ext or "")
    ext = os.path.splitext("x" + s if s.startswith(".") else s if "." in s else "x." + s)[1].lower()
    if not re.match(r"^\.[\w\-+~]{1,24}$", ext):
        ext = ".zzunknown"
    return _uri(_mem_get(("type", ext), lambda: _shell_png("x" + ext)))


def stock_icon(name):
    """Data URI of a Windows stock icon: 'settings' (services), 'application', 'folder', 'drive', 'printer'…"""
    if name == "settings":  # the stock "settings" icon is drawn as a black square at 48 px: Windows' own gear
        shell32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shell32.dll")
        gear = _uri(_mem_get(("gear",), lambda: _hicon_png(shell32, -16826)))
        if gear:
            return gear
    siid = STOCK.get(name)
    if siid is None:
        return None

    def make():
        info = _SHSTOCKICONINFO()
        info.cbSize = ctypes.sizeof(info)
        _com()
        if _shell32.SHGetStockIconInfo(siid, 0x4000, ctypes.byref(info)) != 0:  # SHGSI_SYSICONINDEX
            return None
        data = _index_png(info.iSysImageIndex)
        return None if _looks_blank(data) else data
    return _uri(_mem_get(("stock", name), make))


def windows_icon():
    """The Windows logo, for parts of Windows that have no program file (System, Registry, MemCompression…)."""
    imageres = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "imageres.dll")
    return _uri(_mem_get(("win",), lambda: _hicon_png(imageres, -5308))) or stock_icon("desktop")


def file_icon(path):
    """The file's own icon (programs, shortcuts, .ico) or, for documents, the icon of its type."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in (".exe", ".ico", ".lnk", ".cpl", ".scr", ".msc") and os.path.isfile(path):
        own = icon_for_file(path)
        if own:
            return own
    return type_icon(ext or ".zzunknown")

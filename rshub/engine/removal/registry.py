"""Registry access with 32/64-bit views, .reg backups and recursive deletion."""
import os
import subprocess
import winreg
from dataclasses import dataclass

NOWIN = 0x08000000  # CREATE_NO_WINDOW
REG_TIMEOUT = 300   # seconds for one reg.exe export/import: a hung one must not hold a removal or an undo forever
V64, V32 = winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY

HIVES = {
    "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    "HKCU": winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKU": winreg.HKEY_USERS, "HKEY_USERS": winreg.HKEY_USERS,
    "HKCR": winreg.HKEY_CLASSES_ROOT, "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
}
SHORT = {winreg.HKEY_LOCAL_MACHINE: "HKLM", winreg.HKEY_CURRENT_USER: "HKCU",
         winreg.HKEY_USERS: "HKU", winreg.HKEY_CLASSES_ROOT: "HKCR"}


@dataclass(frozen=True)
class RegRef:
    hive: str          # "HKLM" | "HKCU" | "HKU"
    path: str          # without the hive
    view: int = 0      # 0, V64 or V32
    value: str = None  # None = the whole key

    @property
    def root(self):
        return HIVES[self.hive]

    @property
    def full(self):
        return f"{self.hive}\\{self.path}"

    def display(self):
        p = self.full
        if self.view == V32 and self.hive == "HKLM" and self.path.lower().startswith("software\\"):
            p = f"HKLM\\SOFTWARE\\WOW6432Node\\{self.path[9:]}"
        return p + (f"  →  {self.value}" if self.value is not None else "")

    def key(self):
        """Stable identifier (used to deduplicate)."""
        return f"{self.display().lower()}"

    def to_str(self):
        return f"{self.hive}|{self.path}|{self.view}|{'' if self.value is None else self.value}|{int(self.value is not None)}"

    @staticmethod
    def from_str(s):
        hive, path, view, value, has_value = s.split("|", 4)
        return RegRef(hive, path, int(view), value if has_value == "1" else None)


def open_key(ref_or_root, path=None, view=0, access=winreg.KEY_READ):
    if isinstance(ref_or_root, RegRef):
        return winreg.OpenKey(ref_or_root.root, ref_or_root.path, 0, access | ref_or_root.view)
    return winreg.OpenKey(ref_or_root, path, 0, access | view)


def key_exists(ref):
    try:
        winreg.CloseKey(open_key(RegRef(ref.hive, ref.path, ref.view)))
        return True
    except OSError:
        return False


def value_exists(ref):
    try:
        with open_key(ref) as k:
            winreg.QueryValueEx(k, ref.value)
        return True
    except OSError:
        return False


def subkeys(root, path, view=0):
    try:
        k = open_key(root, path, view)
    except OSError:
        return []
    out = []
    with k:
        i = 0
        while True:
            try:
                out.append(winreg.EnumKey(k, i))
            except OSError:
                break
            i += 1
    return out


def values(root, path, view=0):
    """List of (name, data, type)."""
    try:
        k = open_key(root, path, view)
    except OSError:
        return []
    out = []
    with k:
        i = 0
        while True:
            try:
                out.append(winreg.EnumValue(k, i))
            except OSError:
                break
            i += 1
    return out


def get_value(root, path, name, view=0, default=None):
    try:
        with open_key(root, path, view) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


def read_value_full(ref):
    """(data, type) of a value, or None."""
    try:
        with open_key(ref) as k:
            return winreg.QueryValueEx(k, ref.value)
    except OSError:
        return None


def set_value(ref, data, typ):
    with winreg.CreateKeyEx(ref.root, ref.path, 0, winreg.KEY_SET_VALUE | ref.view) as k:
        winreg.SetValueEx(k, ref.value, 0, typ, data)


def delete_value(ref):
    with open_key(ref, access=winreg.KEY_SET_VALUE) as k:
        winreg.DeleteValue(k, ref.value)


def delete_tree(ref):
    """Delete a key and all its subkeys in the given view."""
    root, path, view = ref.root, ref.path, ref.view
    try:
        k = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS | view)
    except FileNotFoundError:
        return
    with k:
        while True:
            try:
                child = winreg.EnumKey(k, 0)
            except OSError:
                break
            delete_tree(RegRef(ref.hive, path + "\\" + child, view))
    winreg.DeleteKeyEx(root, path, view, 0)


def export_key(ref, out_file):
    """Export the key to a .reg file. Return True if the file was created."""
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    args = ["reg", "export", RegRef(ref.hive, ref.path).full, out_file, "/y"]
    if ref.view == V32:
        args.append("/reg:32")
    elif ref.view == V64:
        args.append("/reg:64")
    try:
        r = subprocess.run(args, capture_output=True, creationflags=NOWIN, timeout=REG_TIMEOUT)
    except subprocess.TimeoutExpired:   # reg.exe is killed; a half-written file is not a backup
        try:
            os.remove(out_file)
        except OSError:
            pass
        return False
    return r.returncode == 0 and os.path.exists(out_file)


def import_reg(reg_file, view=0):
    args = ["reg", "import", reg_file]
    if view == V32:
        args.append("/reg:32")
    elif view == V64:
        args.append("/reg:64")
    # bytes, decoded here: reg.exe writes its localized message in the console code page, and text=True decoded it as
    # ANSI and raised on some letters ("Überprüfen", "É"), turning a restore that worked into an error
    try:
        r = subprocess.run(args, capture_output=True, creationflags=NOWIN, timeout=REG_TIMEOUT)
    except subprocess.TimeoutExpired:
        from ...i18n import tr
        return False, tr("Timed out")
    return r.returncode == 0, _decode(r.stderr or r.stdout).strip()


def _decode(raw):
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("oem", "replace")


def walk_values(root, path, view=0, max_depth=6, _depth=0):
    """Generator of (subpath, name, data) walking the subkeys."""
    for name, data, _ in values(root, path, view):
        yield path, name, data
    if _depth >= max_depth:
        return
    for s in subkeys(root, path, view):
        yield from walk_values(root, path + "\\" + s, view, max_depth, _depth + 1)


def hklm_views():
    return (V64, V32)

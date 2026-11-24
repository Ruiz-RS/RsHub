"""Backups and restore engine: Windows restore points and System Protection, plus RsHub's own backups
(Super Optimizer, Driver Updater, Program Remover quarantine) listed in one place.

The shared helper every module should use to make a restore point:

    from .backups import create_restore_point
    rp = create_restore_point(tr("RsHub: before installing drivers"), kind="DEVICE_DRIVER_INSTALL")
    # {"ok": bool, "reason": code, "detail": Windows' own text or "", "msg": translated explanation, "seq": n}

reason codes: "created", "needs_admin", "protection_off" (System Protection is off for the system drive),
"service_disabled" (Volume Shadow Copy or its provider is disabled), "policy" (turned off by a policy) and
"failed" (Windows' message in detail, or no new point appeared). It lifts Windows' one-point-every-24-hours limit
just for that creation (SystemRestorePointCreationFrequency = 0) and puts the previous value back, or removes it if
it did not exist. The previous value is written to a small file first, so a crash in between is repaired next time.

Whatever Windows only shows to administrators (restore points, shadow storage) comes back as None in normal mode.
Nothing here deletes more than the single item it is given.
"""
import base64
import contextlib
import ctypes
import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
import winreg

from ..i18n import tr
from .optimizer import USER_DATA
from .ps import NO_WINDOW, run

EXPORTS = {"is_admin", "open_path", "reason_text"}
JOBS = {"status", "own_backups", "create_point", "protect", "delete_point", "delete_own"}

DATA = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "RsHub")
OPT_DIR = os.path.join(DATA, "optimizer")
# Per-user Super Optimizer copies (HKCU registry fixes, network settings) are in the user's profile.
USER_OPT_DIR = USER_DATA
# Folders of OPT_DIR that are not backups: Debloat's state (its undo records, removed apps), managed by Debloat.
OPT_STATE = ("debloat",)
MARKER = os.path.join(DATA, "backups", "rp_frequency.json")

SR_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore"
FREQ = "SystemRestorePointCreationFrequency"
SPP_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SPP\Clients"
SPP_SR = "{09F7EDC5-294E-4180-AF6A-FB0E6A0E9513}"  # the System Restore client: one entry per protected volume
POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Windows NT\SystemRestore"
SERVICES = ("VSS", "swprv", "srservice")  # srservice only exists on old Windows; missing is fine
UNBOUNDED = 2 ** 64 - 1
KINDS = ("APPLICATION_INSTALL", "APPLICATION_UNINSTALL", "DEVICE_DRIVER_INSTALL", "MODIFY_SETTINGS", "CANCELLED_OPERATION")
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

_RP_LOCK = threading.RLock()  # one creation at a time: two overlapping overrides would lose the original value


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ======================================================================= messages
def reason_text(reason, detail="", drive=None):
    """Translated explanation for a reason code (other modules show it as is)."""
    drive = drive or _system_drive()
    if reason == "created":
        return tr("Restore point created.")
    if reason == "needs_admin":
        return tr("Creating a restore point needs administrator rights.")
    if reason == "protection_off":
        return tr("System Protection is turned off for drive {drive}, so Windows cannot create restore points.", drive=drive)
    if reason == "service_disabled":
        return tr("The Volume Shadow Copy service is disabled, and restore points depend on it.")
    if reason == "policy":
        return tr("Restore points are turned off by a policy on this PC.")
    if detail:
        return tr("Windows could not create it: {detail}", detail=detail)
    return tr("Windows did not create a new restore point and gave no reason.")


def _res(ok, reason, detail="", **extra):
    out = {"ok": bool(ok), "reason": reason, "detail": str(detail or "").strip()[:400]}
    out["msg"] = reason_text(reason, out["detail"])
    out.update(extra)
    return out


# ======================================================================= pure parsers (tested)
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_wmi_time(s):
    """CIM datetime ("20261118163012.123456-300"), "/Date(ms)/" or ISO 8601 -> epoch seconds. None if unreadable."""
    if not s:
        return None
    s = str(s).strip()
    m = re.fullmatch(r"(\d{14})(?:\.(\d{1,6}))?([+-])(\d{3})", s)
    if m:
        dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=datetime.timezone.utc)
        if m.group(2):
            dt += datetime.timedelta(microseconds=int(m.group(2).ljust(6, "0")))
        off = int(m.group(4)) * (1 if m.group(3) == "+" else -1)  # minutes east of UTC
        return (dt - datetime.timedelta(minutes=off)).timestamp()
    m = re.fullmatch(r"/Date\((-?\d+)\)/", s)
    if m:
        return int(m.group(1)) / 1000
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def parse_restore_points(rows):
    """Rows of Get-ComputerRestorePoint ({seq, desc, type, t} or the raw property names) -> newest first."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        seq = _int(r.get("seq", r.get("SequenceNumber")))
        if seq is None:
            continue
        out.append({"seq": seq, "desc": str(r.get("desc", r.get("Description")) or ""),
                    "type": _int(r.get("type", r.get("RestorePointType"))),
                    "t": parse_wmi_time(r.get("t", r.get("CreationTime")))})
    return sorted(out, key=lambda p: p["seq"], reverse=True)


def parse_spp_clients(entries):
    """SPP\\Clients System Restore value (REG_MULTI_SZ) -> {volume guid (lower case): drive letter or None}.
    An entry looks like  \\\\?\\Volume{56580811-...}\\:(C%3A)"""
    out = {}
    for e in entries or []:
        m = re.search(r"Volume\{(" + _GUID + r")\}", str(e))
        if not m:
            continue
        d = re.search(r"\(([A-Za-z])%3A\)", str(e))
        out[m.group(1).lower()] = (d.group(1).upper() + ":") if d else None
    return out


def parse_size(s):
    """'4.66 GB (5%)', '4,66 GB', '0 bytes', '512 KB', '1,5 Go' -> bytes. None for anything else (UNBOUNDED...)."""
    m = re.search(r"(\d[\d.,\u00a0\u202f ]*)\s*(bytes?|octets?|KB|MB|GB|TB|PB|Ko|Mo|Go|To|B)(?![A-Za-z])", str(s), re.I)
    if not m:
        return None
    num = re.sub(r"[\u00a0\u202f ]", "", m.group(1))
    if "," in num and "." in num:
        dec = "," if num.rfind(",") > num.rfind(".") else "."
        num = num.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in num:
        num = num.replace(",", ".")
    try:
        v = float(num)
    except ValueError:
        return None
    u = m.group(2).upper()
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}.get(u[0], 1)
    if u.startswith(("BYTE", "OCTET")) or u == "B":
        mult = 1
    return int(round(v * mult))


def parse_pct(s):
    m = re.search(r"\((\d+(?:[.,]\d+)?)\s*%\)", str(s))
    return float(m.group(1).replace(",", ".")) if m else None


def parse_shadowstorage(text):
    """`vssadmin list shadowstorage` -> [{drive, vol, on, used, alloc, max, maxPct}].
    Windows translates the labels, so this reads by position: every association is two volume lines ("for volume",
    "storage volume") followed by the used, allocated and maximum space."""
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    vol_re = re.compile(r"(?:\(([A-Za-z]):\))?\s*\\\\\?\\Volume\{(" + _GUID + r")\}")
    out, i = [], 0
    while i < len(lines):
        m1 = vol_re.search(lines[i])
        m2 = vol_re.search(lines[i + 1]) if i + 1 < len(lines) else None
        vals = lines[i + 2:i + 5]
        if m1 and m2 and len(vals) == 3 and all(":" in v for v in vals):
            used, alloc, mx = (v.split(":", 1)[1] for v in vals)
            out.append({"drive": (m1.group(1).upper() + ":") if m1.group(1) else None, "vol": m1.group(2).lower(),
                        "on": (m2.group(1).upper() + ":") if m2.group(1) else None,
                        "used": parse_size(used), "alloc": parse_size(alloc), "max": parse_size(mx), "maxPct": parse_pct(mx)})
            i += 5
            continue
        i += 1
    return out


def storage_from_cim(rows):
    """Win32_ShadowStorage rows ({vol, diff, used, alloc, max}) -> the same shape as parse_shadowstorage."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        m = re.search(r"Volume\{(" + _GUID + r")\}", str(r.get("vol") or ""))
        if not m:
            continue
        mx = _int(r.get("max"))
        out.append({"drive": None, "vol": m.group(1).lower(), "on": None, "used": _int(r.get("used")),
                    "alloc": _int(r.get("alloc")), "max": None if mx is None or mx >= UNBOUNDED else mx, "maxPct": None})
    return out


def match_shadows(points, shadows, system_vol, tolerance=120):
    """Pairs each restore point with the shadow copy Windows made for it: same system volume, created within
    `tolerance` seconds, and the only candidate (anything ambiguous stays unpaired). -> {seq: shadow id}"""
    cands = []
    for s in shadows or []:
        if not isinstance(s, dict):
            continue
        vol = re.search(r"Volume\{(" + _GUID + r")\}", str(s.get("vol") or ""))
        sid = str(s.get("id") or "").strip().strip("{}")
        ts = parse_wmi_time(s.get("t"))
        if vol and vol.group(1).lower() == (system_vol or "").lower() and ts is not None and re.fullmatch(_GUID, sid):
            cands.append((sid.lower(), ts))
    out = {}
    for p in points or []:
        if p.get("t") is None:
            continue
        near = [c for c in cands if abs(c[1] - p["t"]) <= tolerance]
        if len(near) != 1:
            continue
        # the shadow must also be closest to this point and to no other one
        rivals = [q for q in points if q is not p and q.get("t") is not None and abs(near[0][1] - q["t"]) <= tolerance]
        if not rivals:
            out[p["seq"]] = near[0][0]
    return out


def classify_failure(out, services_off=False):
    """Result of the creation script ({before, after, err, hr, warn}) -> (reason, detail) when no point appeared.
    0x80070422 ("the service cannot be started") is what Windows answers both when VSS is disabled and when System
    Protection is off for the drive: which one depends on whether the services are really disabled."""
    hr = str(out.get("hr") or "").upper()[-8:]
    err = str(out.get("err") or "").strip()
    warn = " ".join(str(w) for w in (out.get("warn") or []) if w).strip()
    if hr in ("80070422", "8007042C") or "0x80070422" in err.lower():
        return ("service_disabled" if services_off else "protection_off"), err
    if hr == "80070005" or "0x80070005" in err.lower():
        return "needs_admin", err
    return "failed", err or warn


# ======================================================================= the 24-hour limit
class _Reg:
    """HKLM\\...\\SystemRestore\\SystemRestorePointCreationFrequency (64-bit view). Tests use a fake."""
    flags = winreg.KEY_WOW64_64KEY

    def read(self):
        """-> (exists, value, type)"""
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SR_KEY, 0, winreg.KEY_READ | self.flags) as k:
                v, typ = winreg.QueryValueEx(k, FREQ)
                return True, v, typ
        except FileNotFoundError:
            return False, None, None

    def write(self, value, typ=winreg.REG_DWORD):
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, SR_KEY, 0, winreg.KEY_SET_VALUE | self.flags) as k:
            winreg.SetValueEx(k, FREQ, 0, typ, value)

    def delete(self):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SR_KEY, 0, winreg.KEY_SET_VALUE | self.flags) as k:
                winreg.DeleteValue(k, FREQ)
        except FileNotFoundError:
            pass


class _Marker:
    """Where the original value waits while the limit is lifted (survives a crash)."""
    def __init__(self, path=MARKER):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) and "exists" in d else None
        except (OSError, ValueError):
            return None

    def save(self, d):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, self.path)

    def clear(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def _put_back(reg, prev):
    if prev.get("exists"):
        reg.write(prev.get("value"), prev.get("type") or winreg.REG_DWORD)
    else:
        reg.delete()


def recover_override(reg=None, marker=None):
    """If an earlier creation was interrupted while the limit was lifted, puts the original value back."""
    reg, marker = reg or _Reg(), marker or _Marker()
    with _RP_LOCK:
        prev = marker.load()
        if prev is None:
            return False
        try:
            _put_back(reg, prev)
        except OSError:
            return False
        marker.clear()
        return True


@contextlib.contextmanager
def frequency_override(reg=None, marker=None):
    """Sets the creation frequency to 0 (no 24-hour limit) inside the block and restores the previous value after,
    even on errors. Yields True if the limit was lifted (False: it could not be written)."""
    reg, marker = reg or _Reg(), marker or _Marker()
    with _RP_LOCK:
        recover_override(reg, marker)
        # a marker still there means recovery could not put the original back: the registry holds OUR 0, and reading
        # it would overwrite the only copy of the user's value with 0 (the limit would stay lifted for good)
        prev = marker.load()
        if prev is None:
            exists, value, typ = reg.read()
            prev = {"exists": bool(exists), "value": value, "type": typ}
        lifted = False
        try:
            marker.save(prev)  # first the copy, then the change
            reg.write(0, winreg.REG_DWORD)
            lifted = True
        except OSError:
            try:
                marker.clear()
            except OSError:
                pass
        try:
            yield lifted
        finally:
            if lifted:
                try:
                    _put_back(reg, prev)
                    marker.clear()
                except OSError:
                    pass  # the marker stays: recover_override() tries again next time


# ======================================================================= Windows queries
def _system_drive():
    return (os.environ.get("SystemDrive") or "C:").upper()[:2]


def _volume_guid(drive):
    buf = ctypes.create_unicode_buffer(64)
    try:
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive[:2] + "\\", buf, 64):
            m = re.search(r"\{(" + _GUID + r")\}", buf.value)
            return m.group(1).lower() if m else None
    except Exception:
        pass
    return None


def _fixed_drives():
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return [_system_drive()]
    out = []
    for i in range(26):
        if mask & (1 << i):
            d = chr(65 + i) + ":"
            if ctypes.windll.kernel32.GetDriveTypeW(d + "\\") == 3:  # DRIVE_FIXED
                out.append(d)
    return out


def _reg_value(key, name):
    """-> value, None if missing; raises PermissionError when Windows denies the read."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except FileNotFoundError:
        return None


def services_state():
    """{name: 'auto'|'manual'|'disabled'|...|None (not installed)} from the registry (readable without admin)."""
    names = {0: "boot", 1: "system", 2: "auto", 3: "manual", 4: "disabled"}
    out = {}
    for s in SERVICES:
        try:
            v = _reg_value(r"SYSTEM\CurrentControlSet\Services" + "\\" + s, "Start")
        except OSError:
            v = None
        out[s] = names.get(v) if v is not None else None
    return out


def _services_off():
    svc = services_state()
    return [s for s in SERVICES if svc.get(s) == "disabled"]


def policy_blocked():
    try:
        return _reg_value(POLICY_KEY, "DisableSR") == 1
    except OSError:
        return False


def protected_volumes():
    """{guid: letter} of the volumes with System Protection on. None if it cannot be read (normal mode)."""
    try:
        v = _reg_value(SPP_KEY, SPP_SR)
    except OSError:
        return None
    if v is None:
        return {}
    return parse_spp_clients(v if isinstance(v, (list, tuple)) else [v])


def _ps_obj(script, timeout=120):
    """Runs a PowerShell script that ends in one object and returns it parsed (None if there is no answer)."""
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                            + script.strip() + " | ConvertTo-Json -Depth 5 -Compress"],
                           capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.decode("utf-8", "replace").strip()
    if "{" not in out:
        return None
    try:
        return json.loads(out[out.find("{"):])
    except ValueError:
        return None


_QUERY = r"""
$o = [ordered]@{ points=$null; perr=$null; storage=$null; serr=$null; shadows=$null }
try { $o.points = @(Get-ComputerRestorePoint -ErrorAction Stop | ForEach-Object { [pscustomobject]@{ seq=[int]$_.SequenceNumber; desc=[string]$_.Description; type=[int]$_.RestorePointType; t=[string]$_.CreationTime } }) } catch { $o.perr = $_.Exception.Message }
try { $o.storage = @(Get-CimInstance Win32_ShadowStorage -ErrorAction Stop | ForEach-Object { [pscustomobject]@{ vol=[string]$_.Volume.DeviceID; diff=[string]$_.DiffVolume.DeviceID; used=[uint64]$_.UsedSpace; alloc=[uint64]$_.AllocatedSpace; max=[uint64]$_.MaxSpace } }) } catch { $o.serr = $_.Exception.Message }
try { $o.shadows = @(Get-CimInstance Win32_ShadowCopy -ErrorAction Stop | ForEach-Object { [pscustomobject]@{ id=[string]$_.ID; vol=[string]$_.VolumeName; t=$_.InstallDate.ToUniversalTime().ToString('o') } }) } catch { }
[pscustomobject]$o
"""


def _query():
    """Restore points, shadow storage and shadow copies (administrator only)."""
    q = _ps_obj(_QUERY, timeout=90) or {"perr": tr("PowerShell did not answer.")}
    storage = storage_from_cim(q.get("storage")) if q.get("storage") is not None else None
    if storage is None:  # WMI failed: the console tool reports the same thing as text
        code, txt = run(["vssadmin", "list", "shadowstorage"], timeout=60, encoding="oem")
        storage = parse_shadowstorage(txt) if code == 0 else []
    return {"points": parse_restore_points(q.get("points")) if q.get("perr") is None else None,
            "perr": q.get("perr"), "storage": storage, "shadows": q.get("shadows") or []}


def _drive_rows(protected, storage):
    sysd = _system_drive()
    rows = []
    for d in _fixed_drives():
        guid = _volume_guid(d)
        try:
            total = shutil.disk_usage(d + "\\").total
        except OSError:
            total = None
        st = next((s for s in storage or [] if s["vol"] == guid or s.get("drive") == d), None)
        mx = st.get("max") if st else None
        pct = st.get("maxPct") if st else None
        rows.append({"drive": d, "system": d == sysd, "total": total,
                     "enabled": None if protected is None else (guid in protected),
                     "used": st.get("used") if st else None, "max": mx,
                     "maxPct": pct if pct is not None else (round(mx / total * 100, 1) if mx and total else None)})
    rows.sort(key=lambda r: (not r["system"], r["drive"]))
    return rows


def _precheck():
    """-> (reason, detail) that stops a creation before trying, or None."""
    if not is_admin():
        return "needs_admin", ""
    if policy_blocked():
        return "policy", ""
    off = _services_off()
    if off:
        return "service_disabled", ", ".join(off)
    prot = protected_volumes()
    guid = _volume_guid(_system_drive())
    if prot is not None and guid and guid not in prot:
        return "protection_off", ""
    return None


# ======================================================================= the shared helper
_CREATE = r"""
$d = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__DESC__'))
$r = [ordered]@{ before=$null; after=$null; desc=$null; err=$null; hr=$null; warn=@() }
try { $p = @(Get-ComputerRestorePoint -ErrorAction Stop); if ($p.Count) { $r.before = [int](($p | Measure-Object SequenceNumber -Maximum).Maximum) } } catch { }
$wv = @()
try { Checkpoint-Computer -Description $d -RestorePointType __KIND__ -ErrorAction Stop -WarningVariable wv -WarningAction SilentlyContinue }
catch { $r.err = $_.Exception.Message; $r.hr = '{0:X8}' -f $_.Exception.HResult }
$r.warn = @($wv | ForEach-Object { [string]$_ })
try { $l = Get-ComputerRestorePoint -ErrorAction Stop | Sort-Object SequenceNumber | Select-Object -Last 1; if ($l) { $r.after = [int]$l.SequenceNumber; $r.desc = [string]$l.Description } } catch { }
[pscustomobject]$r
"""


def create_restore_point(description, kind="MODIFY_SETTINGS", progress=None):
    """Creates a restore point now, even if one was made in the last 24 hours. See the module docstring."""
    say = progress or (lambda *a: None)
    desc = re.sub(r"\s+", " ", str(description or "")).strip()[:120] or "RsHub"
    kind = kind if kind in KINDS else "MODIFY_SETTINGS"
    say(.05, tr("Checking System Protection"))
    stop = _precheck()
    if stop:
        return _res(False, *stop)
    script = _CREATE.replace("__DESC__", base64.b64encode(desc.encode("utf-8")).decode("ascii")).replace("__KIND__", kind)
    with _RP_LOCK, frequency_override() as lifted:
        say(.2, tr("Creating the restore point"))
        out = _ps_obj(script, timeout=420) or {}
    say(.9, tr("Checking that it was created"))
    before, after = _int(out.get("before")), _int(out.get("after"))
    if after is not None and (before is None or after > before):
        return _res(True, "created", seq=after, desc=out.get("desc") or desc, lifted=lifted)
    if not out:
        return _res(False, "failed", tr("PowerShell did not answer."), lifted=lifted)
    reason, detail = classify_failure(out, services_off=bool(_services_off()))
    if reason == "failed" and not detail and not lifted:
        detail = tr("RsHub could not lift the one-per-day limit.")
    return _res(False, reason, detail, lifted=lifted)


# ======================================================================= jobs: restore points
def status(job):
    """Everything the section shows about restore points and System Protection."""
    admin = is_admin()
    job.progress(.1, tr("Reading System Protection"))
    if admin:
        recover_override()
    out = {"admin": admin, "systemDrive": _system_drive(), "services": services_state(), "policy": policy_blocked(),
           "points": None, "drives": None, "error": None}
    if not admin:
        out["drives"] = _drive_rows(None, None)
        job.progress(1, tr("Done"))
        return out
    job.progress(.3, tr("Reading the restore points"))
    q = _query()
    out["drives"] = _drive_rows(protected_volumes(), q["storage"])
    if q["points"] is None:
        out["error"] = q.get("perr")
        out["points"] = []
    else:
        pairs = match_shadows(q["points"], q["shadows"], _volume_guid(_system_drive()))
        for p in q["points"]:
            p["shadow"] = pairs.get(p["seq"])
        out["points"] = q["points"]
    job.progress(1, tr("Done"))
    return out


def create_point(job, name=None):
    name = str(name or "").strip() or tr("RsHub, manual restore point")
    return create_restore_point(name, "MODIFY_SETTINGS", progress=lambda p, t: job.progress(p, t, force=True))


def protect(job, drive=None, pct=10):
    """Turns System Protection on for a drive, or changes its space: Enable-ComputerRestore + vssadmin resize."""
    if not is_admin():
        return {"ok": False, "reason": "needs_admin", "msg": tr("Turning on System Protection needs administrator rights.")}
    drive = str(drive or _system_drive())[:1].upper() + ":"
    if not re.fullmatch(r"[A-Z]:", drive) or drive not in _fixed_drives():
        return {"ok": False, "reason": "failed", "msg": tr("That drive is not available.")}
    try:
        pct = max(1, min(50, int(pct)))
    except (TypeError, ValueError):
        pct = 10
    if policy_blocked():
        return {"ok": False, "reason": "policy", "msg": reason_text("policy")}
    if _services_off():
        return {"ok": False, "reason": "service_disabled", "msg": reason_text("service_disabled")}
    guid = _volume_guid(drive)
    err = ""
    if guid not in (protected_volumes() or {}):
        job.progress(.2, tr("Turning on System Protection for {drive}", drive=drive), force=True)
        r = _ps_obj("$e=$null; try { Enable-ComputerRestore -Drive '" + drive + "\\' -ErrorAction Stop } "
                    "catch { $e=$_.Exception.Message }\n[pscustomobject]@{ err=$e }", timeout=120) or {}
        err = r.get("err") or ""
    job.progress(.6, tr("Setting the space for restore points"), force=True)
    code, txt = run(["vssadmin", "resize", "shadowstorage", f"/for={drive}", f"/on={drive}", f"/maxsize={pct}%"],
                    timeout=120, encoding="oem")
    enabled = guid in (protected_volumes() or {})
    job.progress(1, tr("Done"), force=True)
    if not enabled:
        detail = err or ((txt or "").strip().splitlines() or [tr("no reason given")])[-1]
        return {"ok": False, "reason": "failed", "msg": tr("Windows did not turn it on: {detail}", detail=detail[:300])}
    return {"ok": True, "enabled": True, "resized": code == 0, "pct": pct,
            "msg": tr("System Protection is on for {drive}.", drive=drive) if code == 0 else
            tr("System Protection is on for {drive}, but Windows kept the previous space.", drive=drive)}


def delete_point(job, seq, shadow=None):
    """Deletes ONE restore point, by its sequence number. Never more than that one."""
    if not is_admin():
        return {"ok": False, "reason": "needs_admin", "msg": tr("Deleting a restore point needs administrator rights.")}
    seq = _int(seq)
    job.progress(.15, tr("Finding the restore point"), force=True)
    q = _query()
    pts = q["points"] or []
    if seq is None or not any(p["seq"] == seq for p in pts):
        return {"ok": False, "reason": "failed", "msg": tr("That restore point no longer exists.")}
    sid = match_shadows(pts, q["shadows"], _volume_guid(_system_drive())).get(seq)
    if shadow and sid and str(shadow).strip("{}").lower() != sid:
        return {"ok": False, "reason": "failed", "msg": tr("The list changed. Refresh it and try again.")}
    job.progress(.45, tr("Deleting the restore point"), force=True)
    detail = ""
    if sid:  # the shadow copy Windows made for exactly this point (never /all, never /for=)
        code, txt = run(["vssadmin", "delete", "shadows", "/shadow={" + sid + "}", "/quiet"], timeout=180, encoding="oem")
        if code != 0:
            detail = ((txt or "").strip().splitlines() or [""])[-1]
    else:  # no unambiguous shadow copy: the System Restore API removes just this sequence number
        try:
            fn = ctypes.windll.srclient.SRRemoveRestorePoint
            fn.argtypes, fn.restype = [ctypes.c_uint32], ctypes.c_uint32
            code = fn(seq)
        except Exception as e:
            code, detail = -1, str(e)
        if code and not detail:
            detail = tr("Windows returned error {code}.", code=code)
    job.progress(.85, tr("Checking"), force=True)
    gone = not any(p["seq"] == seq for p in (_query()["points"] or []))
    job.progress(1, tr("Done"), force=True)
    if gone:
        return {"ok": True, "seq": seq, "msg": tr("Restore point deleted.")}
    return {"ok": False, "reason": "failed", "msg": tr("Windows did not delete it: {detail}", detail=detail or tr("no reason given"))}


# ======================================================================= RsHub's own backups
def _size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    n = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def _bid_time(bid, fallback):
    try:
        return time.mktime(time.strptime(bid[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return fallback


def optimizer_backups(base=None):
    """Super Optimizer copies: those in ProgramData\\RsHub\\optimizer (services, HKLM registry keys) and the
    user's own in %LOCALAPPDATA%\\RsHub\\optimizer (HKCU registry keys, network settings). One registry fix
    that touched both is a single entry (same id). `base` (tests) lists just that folder."""
    merged = {}
    for root in ([base] if base else [OPT_DIR, USER_OPT_DIR]):
        for b in _optimizer_backups_in(root):
            m = merged.get(b["id"])
            if m is None:
                b["paths"] = [b["path"]]
                merged[b["id"]] = b
            else:
                m["paths"].append(b["path"])
                m["size"] += b["size"]
                m["count"] = (m["count"] or 0) + (b["count"] or 0)
                m["sample"] = (m["sample"] + b["sample"])[:3]
    return sorted(merged.values(), key=lambda b: b["ts"] or 0, reverse=True)


def _optimizer_backups_in(base):
    """Super Optimizer copies in one folder: registry (.reg + manifest) and services (.json). Any other folder
    there (added later) is listed as a generic backup, except Debloat's state (its undo records are not a backup:
    deleting them would stop its changes from being undone). Startup changes keep no copy: they are switched off
    the way Task Manager does it and turned back on from the Startup tab."""
    out = []
    reg = os.path.join(base, "registry")
    for bid in sorted(os.listdir(reg)) if os.path.isdir(reg) else []:
        p = os.path.join(reg, bid)
        if not os.path.isdir(p):
            continue
        try:
            with open(os.path.join(p, "manifest.json"), encoding="utf-8") as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            man = []
        man = man if isinstance(man, list) else []
        out.append({"id": "opt-registry|" + bid, "kind": "opt-registry", "bid": bid, "path": p, "folder": p,
                    "ts": _bid_time(bid, os.path.getmtime(p)), "size": _size(p), "count": len(man),
                    "sample": [str(m.get("k", "")) for m in man[:3] if isinstance(m, dict)]})
    svc = os.path.join(base, "services")
    for f in sorted(os.listdir(svc)) if os.path.isdir(svc) else []:
        p = os.path.join(svc, f)
        if not (f.endswith(".json") and os.path.isfile(p)):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            prev = d.get("prev") or {}
        except (OSError, ValueError, AttributeError):
            d, prev = {}, {}
        bid = f[:-5]
        out.append({"id": "opt-services|" + bid, "kind": "opt-services", "bid": bid, "path": p, "folder": p,
                    "ts": d.get("t") or _bid_time(bid, os.path.getmtime(p)), "size": _size(p), "count": len(prev),
                    "sample": sorted(prev)[:3]})
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        p = os.path.join(base, name)
        if name in ("registry", "services") or name in OPT_STATE or not os.path.isdir(p):
            continue
        for sub in sorted(os.listdir(p)):
            sp = os.path.join(p, sub)
            out.append({"id": "opt-other|" + name + "|" + sub, "kind": "opt-other", "group": name, "bid": sub, "path": sp,
                        "folder": sp, "ts": os.path.getmtime(sp), "size": _size(sp), "count": None, "sample": []})
    return out


def driver_backups():
    from . import drivers
    return [{"id": "driver|" + b["path"], "kind": "driver", "path": b["path"], "folder": b["path"], "ts": b["ts"],
             "size": int(round((b.get("gb") or 0) * 1024 ** 3)), "count": b.get("count")} for b in drivers.backups()]


def quarantine_sessions():
    """Program Remover quarantine, read only (restoring and deleting live in the Program Remover)."""
    from .removal import paths
    root = os.path.join(paths.DATA_DIR, "quarantine")
    if not os.path.isdir(root):  # do not create it just to list it
        return [], root
    from .removal import quarantine
    return [{"id": "quarantine|" + s["id"], "kind": "quarantine", "sid": s["id"], "program": s.get("program") or "",
             "path": os.path.join(root, s["id"]), "folder": os.path.join(root, s["id"]), "ts": s.get("created"),
             "size": s.get("size") or 0, "count": s.get("count") or 0, "restorable": bool(s.get("restorable")),
             "restored": bool(s.get("restored"))} for s in quarantine.sessions()], root


def own_backups(job):
    from . import drivers
    job.progress(.1, tr("Reading the Super Optimizer backups"))
    opt = optimizer_backups()
    job.progress(.4, tr("Reading the driver backups"))
    try:
        drv = driver_backups()
    except Exception:
        drv = []
    job.progress(.8, tr("Reading the quarantine"))
    try:
        quar, qroot = quarantine_sessions()
    except Exception:
        quar, qroot = [], None
    job.progress(1, tr("Done"))
    return {"optimizer": opt, "drivers": drv, "quarantine": quar,
            "roots": {"optimizer": OPT_DIR, "optimizerUser": USER_OPT_DIR, "drivers": drivers.default_backup_dir(), "quarantine": qroot}}


def _recycle(path):
    """Sends a file or folder to the Recycle Bin (it can be recovered from there)."""
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW(None, 3, path + "\0\0", None, 0x0040 | 0x0010 | 0x0004 | 0x0400, False, None, None)  # FO_DELETE, ALLOWUNDO|NOCONFIRMATION|SILENT|NOERRORUI
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return r == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)


def delete_own(job, ids):
    """Sends the chosen RsHub backups to the Recycle Bin. Only ids from the current listing are accepted, and
    never quarantine sessions (the Program Remover manages those)."""
    ids = [str(i) for i in (ids or [])][:500]
    known = {b["id"]: b for b in optimizer_backups()}
    try:
        known.update({b["id"]: b for b in driver_backups()})
    except Exception:
        pass
    done, failed = [], []
    for n, i in enumerate(ids):
        job.check()
        b = known.get(i)
        if not b:
            failed.append(i)
            continue
        job.progress(n / max(1, len(ids)), tr("Moving {name} to the Recycle Bin ({n} of {total})",
                                              name=os.path.basename(b["path"]), n=n + 1, total=len(ids)), force=True)
        if b["kind"] == "driver":
            from . import drivers
            ok = bool(drivers.delete_backup(job, b["path"]).get("ok"))
        else:
            ok = all([_recycle(p) for p in (b.get("paths") or [b["path"]])])
        (done if ok else failed).append(i)
    job.progress(1, tr("Done"), force=True)
    return {"ok": not failed, "deleted": done, "failed": failed}


def open_path(path):
    """Opens a backup in File Explorer (a file is shown selected). Only inside RsHub's backup folders."""
    from . import drivers
    from .removal import paths
    p = os.path.normcase(os.path.normpath(str(path or "")))
    roots = [OPT_DIR, USER_OPT_DIR, os.path.join(paths.DATA_DIR, "quarantine"), os.path.dirname(drivers.default_backup_dir())]
    try:
        roots += [b["path"] for b in drivers.backups()]
    except Exception:
        pass
    inside = any(p == r or p.startswith(r + os.sep) for r in (os.path.normcase(os.path.normpath(x)) for x in roots))
    if not inside or not os.path.exists(p):
        return False
    if os.path.isfile(p):
        subprocess.Popen(f'explorer.exe /select,"{p}"', creationflags=NO_WINDOW)
    else:
        os.startfile(p)
    return True

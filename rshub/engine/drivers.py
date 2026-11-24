"""Driver Updater engine. Everything is real:
- inventory: Win32_PnPSignedDriver (+ the state of each device from Win32_PnPEntity)
- search and install: Windows Update (COM Microsoft.Update.Session), signed drivers only, no telemetry
- problems: devices with errors (ConfigManagerErrorCode), disabled and disconnected ("ghost") devices
- backup: pnputil /export-driver and /add-driver
- history: Windows Update history filtered to drivers
- stored versions and rollback: pnputil /enum-devices /drivers + UpdateDriverForPlugAndPlayDevices
- DriverStore cleanup: old, unused copies of updated drivers (pnputil /enum-drivers /devices; /delete-driver without /force)
Anything that changes the system needs administrator rights: checked here, returning {"needAdmin": True}.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time

from ..i18n import tr
from .jobs import Cancelled
from .ps import ps_json, run, kill_tree, NO_WINDOW

EXPORTS = {"list_drivers", "last_search", "problems", "history", "backups", "default_backup_dir", "pick_folder",
           "open_folder", "driver_versions", "restart", "cancel_restart", "is_admin", "store_packages"}
JOBS = {"search", "install", "export", "restore_backup", "set_enabled", "remove_device", "rollback", "delete_backup", "delete_packages"}

DATA = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "RsHub")
LAST = os.path.join(DATA, "drivers_last_search.json")
KNOWN_BACKUPS = os.path.join(DATA, "drivers_backups.json")
INF_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "INF")

_cache = {"drivers": None, "at": 0}
_lock = threading.Lock()


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _need_admin():
    return None if is_admin() else {"needAdmin": True}


def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _ps_job(job, script, timeout=900):
    """Cancellable PowerShell: if the user cancels, the process is killed."""
    pre = ("$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; ")
    p = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                          "-Command", pre + script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    out = {}

    def reader():
        out["o"], out["e"] = p.communicate()
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t0 = time.monotonic()
    while t.is_alive():
        t.join(0.3)
        if job.cancelled:
            kill_tree(p)
            raise Cancelled()
        if time.monotonic() - t0 > timeout:
            kill_tree(p)
            raise TimeoutError(tr("Windows Update did not respond in time."))
    return (out.get("o") or b"").decode("utf-8", "replace"), (out.get("e") or b"").decode("utf-8", "replace")


def _json_lines(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# ================= inventory =================
_INV = r"""
$st=@{}; Get-CimInstance Win32_PnPEntity -Property DeviceID,ConfigManagerErrorCode | ForEach-Object { $st[$_.DeviceID] = $_.ConfigManagerErrorCode }
Get-CimInstance Win32_PnPSignedDriver -Property DeviceName,DeviceClass,DriverProviderName,DriverVersion,DriverDate,InfName,HardWareID,DeviceID,IsSigned,Manufacturer | Where-Object { $_.DeviceName -and $_.DriverVersion } | ForEach-Object {
  [pscustomobject]@{ n=$_.DeviceName; c="$($_.DeviceClass)"; p=$_.DriverProviderName; v=$_.DriverVersion
    d= if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' }
    inf=$_.InfName; hw=$_.HardWareID; id=$_.DeviceID; s=$_.IsSigned; m=$_.Manufacturer; e=$st[$_.DeviceID] } }
"""


def _inventory_wmi():
    """The rows _INV returns, from two WMI queries inside this process (no powershell.exe)."""
    from . import wmi
    state = {e["DeviceID"]: e["ConfigManagerErrorCode"] for e in wmi.query(r"root\cimv2", "Win32_PnPEntity", ["DeviceID", "ConfigManagerErrorCode"])}
    rows = []
    for d in wmi.query(r"root\cimv2", "Win32_PnPSignedDriver", ["DeviceName", "DeviceClass", "DriverProviderName", "DriverVersion",
                                                                   "DriverDate", "InfName", "HardWareID", "DeviceID", "IsSigned",
                                                                   "Manufacturer"]):
        if not d["DeviceName"] or not d["DriverVersion"]:
            continue
        when = wmi.cim_datetime(d["DriverDate"])
        rows.append({"n": d["DeviceName"], "c": d["DeviceClass"] or "", "p": d["DriverProviderName"], "v": d["DriverVersion"],
                     "d": when.strftime("%Y-%m-%d") if when else "", "inf": d["InfName"], "hw": d["HardWareID"], "id": d["DeviceID"],
                     "s": d["IsSigned"], "m": d["Manufacturer"], "e": state.get(d["DeviceID"])})
    return rows


def _inventory():
    try:
        return _inventory_wmi()
    except Exception as e:
        print("[drivers] WMI failed, using PowerShell:", e, flush=True)
    return ps_json(_INV, timeout=120, as_list=True) or []


def list_drivers(fresh=False, max_age=120):
    """Installed drivers. inbox=True for the ones that ship with Windows (dated 2006-06-21, provider Microsoft)."""
    with _lock:
        if not fresh and _cache["drivers"] and time.time() - _cache["at"] < max_age:
            return _cache["drivers"]
        rows = _inventory()
        out = []
        for i, r in enumerate(rows):
            inbox = (r.get("p") or "").lower() == "microsoft" and (r.get("d") or "").startswith("2006-06-2")
            e = r.get("e")
            out.append({"id": f"d{i}", "name": r.get("n") or "", "cls": (r.get("c") or "OTHER").upper(), "provider": r.get("p") or "",
                        "version": r.get("v") or "", "date": r.get("d") or "", "inbox": inbox, "inf": r.get("inf") or "",
                        "hw": r.get("hw") or "", "instance": r.get("id") or "", "maker": r.get("m") or "",
                        "status": "off" if e == 22 else ("err" if e not in (None, 0) else "ok"), "code": e})
        _cache.update(drivers=out, at=time.time())
        return out


# ================= Windows Update search =================
_SEARCH = r"""
$s = New-Object -ComObject Microsoft.Update.Session
$s.ClientApplicationID = 'RsHub'
$r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Driver' and IsHidden=0")
$out = foreach ($u in $r.Updates) {
  $dv = $null; try { $dv = [datetime]$u.DriverVerDate } catch {}
  [pscustomobject]@{ id=[string]$u.Identity.UpdateID; title=[string]$u.Title; cls=[string]$u.DriverClass
    maker=[string]$u.DriverManufacturer; provider=[string]$u.DriverProvider; model=[string]$u.DriverModel; hw=[string]$u.DriverHardwareID
    date= if ($dv -and $dv.Year -ge 1995) { $dv.ToString('yyyy-MM-dd') } else { '' }
    mb=[math]::Round($u.MaxDownloadSize / 1MB, 1); reboot=[bool]($u.InstallationBehavior.RebootBehavior -ne 0)
    eula=[bool]$u.EulaAccepted }
}
ConvertTo-Json -InputObject @($out) -Depth 4 -Compress
"""


def _ver_from_title(t):
    m = re.findall(r"\d+(?:\.\d+){1,}", t or "")
    return m[-1] if m else ""


def _match(update, drivers):
    hw = (update.get("hw") or "").lower()
    model = (update.get("model") or "").lower()
    best = None
    for d in drivers:
        dh = (d.get("hw") or "").lower()
        if hw and dh and (dh.startswith(hw) or hw.startswith(dh)):
            if best is None or len(dh) > len(best.get("hw") or ""):
                best = d
    if not best and model:
        best = next((d for d in drivers if d["name"].lower() == model), None)
    return best


def search(job):
    """Asks Windows Update which drivers are available for this PC (read-only)."""
    job.progress(.05, tr("Connecting to Windows Update"), phase=0, force=True)
    drivers = []
    t = threading.Thread(target=lambda: drivers.extend(list_drivers()), daemon=True)
    t.start()
    phases = [tr("Requesting the driver catalog for your PC"), tr("Waiting for Windows Update to respond"),
              tr("Comparing with your installed drivers")]
    done = {}

    def run_search():
        try:
            done["out"] = _ps_job(job, _SEARCH, timeout=600)
        except Exception as e:  # noqa: BLE001
            done["err"] = e
    th = threading.Thread(target=run_search, daemon=True)
    th.start()
    i, t0 = 0, time.monotonic()
    while th.is_alive():
        th.join(1.0)
        job.check()
        el = time.monotonic() - t0
        ph = min(len(phases) - 1, int(el // 6))
        if ph != i or el < 2:
            i = ph
        job.progress(min(.85, .1 + el / 90), phases[i] if el > 1 else tr("Connecting to Windows Update"), phase=i + 1, secs=int(el))
    if "err" in done:
        if isinstance(done["err"], Cancelled):
            raise done["err"]
        raise RuntimeError(tr("Windows Update did not respond: {error}", error=done["err"]))
    out, err = done.get("out", ("", ""))
    data = _json_lines(out)
    ups = data[0] if data and isinstance(data[0], list) else (data if data and isinstance(data[0], dict) else [])
    if not data and err.strip():
        raise RuntimeError(tr("Windows Update returned an error: {error}", error=err.strip().splitlines()[-1][:300]))
    t.join(60)
    job.progress(.92, tr("Comparing with your installed drivers"), phase=3)
    res = []
    for u in ups:
        d = _match(u, drivers)
        res.append({"id": u["id"], "title": u["title"], "ver": _ver_from_title(u["title"]), "date": u.get("date") or "",
                    "mb": u.get("mb") or 0, "reboot": bool(u.get("reboot")), "cls": (u.get("cls") or "").upper(),
                    "maker": u.get("maker") or u.get("provider") or "", "model": u.get("model") or "", "hw": u.get("hw") or "",
                    "current": ({"name": d["name"], "version": d["version"], "date": d["date"], "cls": d["cls"],
                                 "provider": d["provider"], "instance": d["instance"]} if d else None)})
    payload = {"at": time.time(), "updates": res}
    _save_json(LAST, payload)
    job.progress(1, tr("Done"), phase=4)
    return payload


def last_search():
    """Result of the last search (saved to disk), without contacting Windows Update."""
    return _load_json(LAST, {"at": None, "updates": []})


# ================= installation =================
_INSTALL = r"""
$ids = '__IDS__' | ConvertFrom-Json
function Say($o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress)); [Console]::Out.Flush() }
$s = New-Object -ComObject Microsoft.Update.Session
$s.ClientApplicationID = 'RsHub'
Say @{ step='search' }
$r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Driver' and IsHidden=0")
$todo = @(); foreach ($u in $r.Updates) { if ($ids -contains [string]$u.Identity.UpdateID) { $todo += $u } }
if ($todo.Count -eq 0) { Say @{ step='none' }; return }
foreach ($u in $todo) {
  $id = [string]$u.Identity.UpdateID
  try {
    if (-not $u.EulaAccepted) { $u.AcceptEula() }
    $col = New-Object -ComObject Microsoft.Update.UpdateColl; [void]$col.Add($u)
    Say @{ step='download'; id=$id }
    $dl = $s.CreateUpdateDownloader(); $dl.Updates = $col; $dr = $dl.Download()
    if ($dr.ResultCode -ne 2 -and $dr.ResultCode -ne 3) { Say @{ step='error'; id=$id; code=$dr.ResultCode; msg='The download failed' }; continue }
    Say @{ step='install'; id=$id }
    $ins = $s.CreateUpdateInstaller(); $ins.Updates = $col; $res = $ins.Install()
    $c = $res.GetUpdateResult(0).ResultCode; $h = '{0:X8}' -f $res.GetUpdateResult(0).HResult
    Say @{ step='done'; id=$id; code=$c; ok=($c -eq 2 -or $c -eq 3); reboot=[bool]$res.RebootRequired; hresult=$h }
  } catch { Say @{ step='error'; id=$id; msg=$_.Exception.Message } }
}
Say @{ step='end' }
"""


def _restore_point(desc):
    """The shared helper (engine/backups.py): lifts the 24-hour limit just for this one, checks that it really
    appeared and says why not -> {ok, reason, detail, msg}."""
    from .backups import create_restore_point
    return create_restore_point(desc, kind="DEVICE_DRIVER_INSTALL")


def install(job, ids, restore_point=True):
    """Installs the selected drivers (Windows Update ids), one by one, creating a restore point first."""
    need = _need_admin()
    if need:
        return need
    ids = [str(i) for i in (ids or []) if re.fullmatch(r"[0-9a-fA-F-]{36}", str(i))]
    if not ids:
        return {"results": [], "reboot": False}
    rp = {"ok": None}
    if restore_point:
        job.progress(.02, tr("Creating a restore point"), step="rp", phase="run", force=True)
        rp = _restore_point(tr("RsHub: before installing drivers"))
        job.progress(.08, tr("Restore point"), step="rp", phase="done" if rp.get("ok") else "skip",
                     reason=rp.get("reason"), msg=None if rp.get("ok") else rp.get("msg"), force=True)
    results, reboot = {}, False
    n = len(ids)

    def on_line(line):
        nonlocal reboot
        for m in _json_lines(line):
            st, uid = m.get("step"), m.get("id")
            k = list(results).index(uid) if uid in results else len(results)
            if st == "search":
                # Windows Update is asked again for the selected ids (30 s to 3 min): say so instead of sitting still
                job.progress(.09, tr("Asking Windows Update for the selected drivers"), step="search", phase="search", force=True)
            elif st == "download":
                results.setdefault(uid, {"id": uid, "ok": False})
                job.progress(.1 + .9 * (k / n), tr("Downloading"), step=uid, phase="download", force=True)
            elif st == "install":
                job.progress(.1 + .9 * ((k + .6) / n), tr("Installing"), step=uid, phase="install", force=True)
            elif st in ("done", "error"):
                r = results.setdefault(uid, {"id": uid})
                msg = m.get("msg")  # "The download failed" from the script, or a Windows exception message (left as is)
                r.update(ok=bool(m.get("ok")), code=m.get("code"), msg=tr(msg) if msg else msg, reboot=bool(m.get("reboot")), hresult=m.get("hresult"))
                reboot = reboot or r["reboot"]
                job.progress(.1 + .9 * ((k + 1) / n), tr("Done") if r["ok"] else tr("Failed"), step=uid, phase="done" if r["ok"] else "error",
                             result=r, force=True)
            elif st == "none":
                job.progress(1, tr("Those drivers are no longer available"), step="search", phase="none", force=True)
    script = _INSTALL.replace("__IDS__", json.dumps(ids))
    try:
        run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; " + script],
            on_line=on_line, job=job, timeout=3600)
    except Exception:
        _prune_installed(results)
        raise
    # what was installed is no longer pending, even if the user cancelled (or it failed) halfway through
    last = _prune_installed(results)
    job.check()
    return {"results": list(results.values()), "reboot": reboot, "restorePoint": rp, "pending": len(last["updates"])}


def _prune_installed(results):
    """Takes the drivers already installed out of the saved last search and the inventory cache."""
    last = last_search()
    okids = {k for k, v in results.items() if v.get("ok")}
    last["updates"] = [u for u in last.get("updates", []) if u["id"] not in okids]
    _save_json(LAST, last)
    _cache["at"] = 0
    return last


def restart(seconds=60):
    subprocess.run(["shutdown", "/r", "/t", str(int(seconds)), "/c", tr("RsHub is restarting the PC to finish installing drivers.")],
                   creationflags=NO_WINDOW)
    return True


def cancel_restart():
    subprocess.run(["shutdown", "/a"], creationflags=NO_WINDOW)
    return True


# ================= problems =================
# Device Manager problem codes in plain words (English keys; translated with tr() when they are sent)
CODES = {
    1: "It is not configured correctly.", 3: "The driver is damaged or the system is low on memory.", 10: "The device cannot start.",
    12: "It cannot find enough free resources.", 14: "The PC needs a restart for it to work.", 18: "Its drivers need to be reinstalled.",
    19: "Its registry configuration is damaged.", 21: "Windows is removing it.", 22: "It is disabled.",
    24: "It is not present, not working properly or missing drivers.", 28: "It has no driver installed.", 29: "It is disabled in the firmware (BIOS).",
    31: "Windows cannot load its drivers.", 32: "Its driver service is disabled.", 37: "The driver failed to start.",
    39: "The driver is missing or damaged.", 41: "Windows loaded the driver but cannot find the device.",
    43: "Windows stopped it because it reported a problem.", 45: "It is not connected right now.", 48: "Windows blocked its driver because of known problems.",
    52: "Windows cannot verify the digital signature of its driver.",
}
_PROBLEMS = r"""
$err = @(Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode<>0' -Property Name,PNPClass,DeviceID,ConfigManagerErrorCode,Manufacturer | ForEach-Object {
  [pscustomobject]@{ name=$_.Name; cls=[string]$_.PNPClass; id=$_.DeviceID; code=$_.ConfigManagerErrorCode; maker=$_.Manufacturer } })
$keep = @('DiskDrive','USB','HIDClass','Printer','Bluetooth','Media','Net','Image','Camera','WPD','Monitor','AudioEndpoint','Keyboard','Mouse','PrintQueue','SmartCardReader','Ports','Biometric','MEDIA','USBDevice','AndroidUsbDeviceClass')
$g = @(Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { -not $_.Present -and $keep -contains $_.Class -and $_.FriendlyName } | Select-Object -First 120)
$last = @{}
if ($g.Count) { try { Get-PnpDeviceProperty -InstanceId $g.InstanceId -KeyName 'DEVPKEY_Device_LastArrivalDate' -ErrorAction SilentlyContinue | ForEach-Object { if ($_.Data) { $last[$_.InstanceId] = ([datetime]$_.Data).ToString('o') } } } catch {} }
$gh = @($g | ForEach-Object { [pscustomobject]@{ name=$_.FriendlyName; cls=[string]$_.Class; id=$_.InstanceId; last=$last[$_.InstanceId] } })
[pscustomobject]@{ errors=$err; ghosts=$gh }
"""


def problems():
    r = ps_json(_PROBLEMS, timeout=120) or {}
    errs = []
    for e in (r.get("errors") or []):
        c = e.get("code")
        if c in (45,):
            continue
        errs.append({**e, "why": tr(CODES[c]) if c in CODES else tr("Windows reports code {code}.", code=c), "disabled": c == 22})
    ghosts = sorted((r.get("ghosts") or []), key=lambda g: g.get("last") or "", reverse=True)
    return {"errors": [e for e in errs if not e["disabled"]], "disabled": [e for e in errs if e["disabled"]], "ghosts": ghosts,
            "at": time.time()}


def set_enabled(job, instance_id, enable):
    need = _need_admin()
    if need:
        return need
    iid = str(instance_id).replace("'", "''")
    cmd = "Enable-PnpDevice" if enable else "Disable-PnpDevice"
    out = ps_json(f"try {{ {cmd} -InstanceId '{iid}' -Confirm:$false -ErrorAction Stop; [pscustomobject]@{{ok=$true}} }} "
                  f"catch {{ [pscustomobject]@{{ok=$false; msg=$_.Exception.Message}} }}", timeout=90) or {}
    _cache["at"] = 0
    return out


def remove_device(job, instance_id):
    """Removes a device (usually one that is no longer connected). Its driver stays on the PC."""
    need = _need_admin()
    if need:
        return need
    code, out = run(["pnputil", "/remove-device", str(instance_id)], encoding="oem", job=job, timeout=120)
    return {"ok": code == 0, "msg": out.strip().splitlines()[-1] if out.strip() else ""}


# ================= history =================
_HIST = r"""
$s = New-Object -ComObject Microsoft.Update.Session; $se = $s.CreateUpdateSearcher()
$n = $se.GetTotalHistoryCount(); if ($n -le 0) { return }
$h = $se.QueryHistory(0, [Math]::Min($n, 400))
$out = foreach ($e in $h) {
  $isDrv = $false
  try { foreach ($c in $e.Categories) { if ($c.Name -eq 'Drivers' -or $c.Type -eq 'UpdateClassification' -and $c.Name -match 'Driver') { $isDrv = $true } } } catch {}
  if (-not $isDrv -and $e.Title -match '^.+ - .+ - [\d.]+$') { $isDrv = $true }
  if ($isDrv -and $e.Operation -eq 1) {
    [pscustomobject]@{ date=$e.Date.ToLocalTime().ToString('o'); title=[string]$e.Title; code=[int]$e.ResultCode; hr=('{0:X8}' -f $e.HResult) } }
}
$out
"""


def history():
    out = ps_json(_HIST, timeout=120, as_list=True) or []
    res = []
    for e in out:
        t = e.get("title") or ""
        parts = [p.strip() for p in t.split(" - ")]
        name = parts[0] if len(parts) >= 3 else t
        res.append({"date": e.get("date"), "title": t, "name": name, "cls": parts[1] if len(parts) >= 3 else "",
                    "ver": _ver_from_title(t), "ok": e.get("code") in (2, 3), "code": e.get("code"),
                    "err": None if e.get("code") in (2, 3) else tr("Windows Update returned 0x{code}", code=e.get("hr"))})
    return res


# ================= backups =================
def default_backup_dir():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            docs = winreg.QueryValueEx(k, "Personal")[0]
    except OSError:
        pass
    return os.path.join(docs, "RsHub", "Driver backups")


def _legacy_backup_dir():
    """Folder used by earlier versions (Spanish name): still listed so old backups are not lost."""
    return os.path.join(os.path.dirname(default_backup_dir()), "Copias de drivers")


PARTIAL = ".partial"  # marker inside a backup folder while it is still being written


def _dir_stats(path):
    n, size = 0, 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if f.lower().endswith(".inf"):
                n += 1
    return n, size


def backups():
    roots = set(_load_json(KNOWN_BACKUPS, []))
    base = default_backup_dir()
    out = []
    cands = set(roots)
    for b in (base, _legacy_backup_dir()):
        if os.path.isdir(b):
            for d in os.listdir(b):
                cands.add(os.path.join(b, d))
    for p in cands:
        if not os.path.isdir(p) or os.path.exists(os.path.join(p, PARTIAL)):
            continue  # a backup that was cancelled or interrupted is never listed as a real one
        n, size = _dir_stats(p)
        if n == 0:
            continue
        out.append({"path": p, "count": n, "gb": round(size / 1024 ** 3, 2), "date": time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p))),
                    "ts": os.path.getmtime(p)})
    return sorted(out, key=lambda b: b["ts"], reverse=True)


def pick_folder(initial=None):
    import webview
    try:
        w = webview.windows[0]
        r = w.create_file_dialog(webview.FOLDER_DIALOG, directory=initial or default_backup_dir())
    except Exception:
        return None
    if not r:
        return None
    return r[0] if isinstance(r, (list, tuple)) else r


def open_folder(path):
    if path and os.path.isdir(path):
        os.startfile(path)
        return True
    return False


def _third_party_count():
    return len([f for f in os.listdir(INF_DIR) if re.fullmatch(r"oem\d+\.inf", f, re.I)]) if os.path.isdir(INF_DIR) else 0


def export(job, folder=None):
    """Exports every manufacturer driver (the Windows ones ship with Windows and cannot be exported)."""
    need = _need_admin()
    if need:
        return need
    base = folder or default_backup_dir()
    # a folder of its own: a second backup in the same minute (a routine right after a manual one) used to write into
    # the first one's folder, and when it was cancelled or failed it removed that folder, the complete backup with it
    stem = dest = os.path.join(base, time.strftime("%Y-%m-%d %H%M"))
    k = 2
    while os.path.exists(dest):
        dest = f"{stem} ({k})"
        k += 1
    os.makedirs(dest, exist_ok=True)
    marker = os.path.join(dest, PARTIAL)
    try:
        open(marker, "w").close()
    except OSError:
        pass
    total = max(1, _third_party_count())
    seen = set()

    def on_line(line):
        for inf in re.findall(r"oem\d+\.inf", line, re.I):
            if inf.lower() not in seen:
                seen.add(inf.lower())
                job.progress(len(seen) / total, line.strip()[:160], n=len(seen), total=total)
    job.progress(0, tr("Preparing the backup"), n=0, total=total, force=True)
    try:
        code, out = run(["pnputil", "/export-driver", "*", dest], on_line=on_line, encoding="oem", job=job, timeout=3600)
        job.check()
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # cancelled or failed: no half backup left behind
        raise
    try:
        os.remove(marker)
    except OSError:
        pass
    n, size = _dir_stats(dest)
    known = set(_load_json(KNOWN_BACKUPS, []))
    if os.path.dirname(dest) != default_backup_dir():
        known.add(dest)
        _save_json(KNOWN_BACKUPS, sorted(known))
    if n == 0:
        try:
            os.rmdir(dest)
        except OSError:
            pass
        return {"ok": False, "msg": out.strip().splitlines()[-1] if out.strip() else tr("pnputil did not export anything.")}
    return {"ok": True, "path": dest, "count": n, "gb": round(size / 1024 ** 3, 2)}


def restore_backup(job, path):
    need = _need_admin()
    if need:
        return need
    if not path or not os.path.isdir(path):
        return {"ok": False, "msg": tr("The backup folder no longer exists.")}
    total = max(1, _dir_stats(path)[0])
    done = [0]

    def on_line(line):
        if re.search(r"\.inf", line, re.I):
            done[0] += 1
            job.progress(min(1, done[0] / total), line.strip()[:160], n=done[0], total=total)
    code, out = run(["pnputil", "/add-driver", os.path.join(path, "*.inf"), "/subdirs", "/install"], on_line=on_line,
                    encoding="oem", job=job, timeout=3600)
    _cache["at"] = 0
    return {"ok": code in (0, 3010), "reboot": code == 3010, "msg": out.strip().splitlines()[-1] if out.strip() else ""}


def delete_backup(job, path):
    """Sends the backup to the Recycle Bin (it can be recovered from there)."""
    known = {b["path"] for b in backups()}
    if path not in known:
        return {"ok": False, "msg": tr("That folder is not a RsHub backup.")}
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW(None, 3, path + "\0\0", None, 0x0040 | 0x0010 | 0x0004 | 0x0400, False, None, None)  # DELETE, ALLOWUNDO|NOCONFIRMATION|SILENT|NOERRORUI
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return {"ok": r == 0 and not op.fAnyOperationsAborted}


# ================= stored versions and rollback =================
def driver_versions(instance_id):
    """Driver packages in the driver store that fit this device (the installed one and older ones)."""
    code, out = run(["pnputil", "/enum-devices", "/instanceid", str(instance_id), "/drivers"], encoding="oem", timeout=60)
    if code != 0:
        return []
    res, cur = [], None
    for line in out.splitlines():
        m = re.search(r":\s*((?:oem\d+|[\w.-]+)\.inf)\s*$", line, re.I)
        if m and line.startswith((" ", "\t")):
            if cur is not None and not cur["version"]:
                cur["original"] = m.group(1)  # "Original name" of the same package, not another package
            else:
                cur = {"inf": m.group(1), "version": "", "date": "", "installed": False, "original": ""}
                res.append(cur)
            continue
        if cur is None:
            continue
        v = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d+(?:\.\d+)+)", line)
        if v:
            cur["date"] = f"{v.group(3)}-{int(v.group(1)):02d}-{int(v.group(2)):02d}"
            cur["version"] = v.group(4)
        # pnputil output is localized: the "status: installed" line in the languages RsHub ships and German
        if re.search(r"instalad|installed|installé|installato|installiert|已安装|已安裝|インストール済み", line, re.I) and \
                re.search(r"estado|status|état|stato|statut|状态|狀態|状態", line, re.I):
            cur["installed"] = True
    # the one to use when rolling back: the newest non-installed package that is older than the installed one
    inst = next((r for r in res if r["installed"]), None)

    def vkey(s):
        return tuple(int(x) for x in re.findall(r"\d+", s or "0"))
    for r in res:
        # only manufacturer packages (oemNN.inf): going back to the generic Windows driver is not "the previous version"
        r["older"] = bool(inst and not r["installed"] and re.fullmatch(r"oem\d+\.inf", r["inf"], re.I)
                          and vkey(r["version"]) < vkey(inst["version"]))
    return res


def rollback(job, instance_id, inf, hwid):
    """Goes back to an older version already in the driver store (forces that INF for the device)."""
    need = _need_admin()
    if need:
        return need
    if not re.fullmatch(r"oem\d+\.inf", str(inf), re.I):
        return {"ok": False, "msg": tr("You can only go back to manufacturer drivers stored on this PC.")}
    path = os.path.join(INF_DIR, inf)
    if not os.path.isfile(path):
        return {"ok": False, "msg": tr("That driver is no longer on this PC.")}
    job.progress(.1, tr("Creating a restore point"), force=True)
    rp = _restore_point(tr("RsHub: before rolling back a driver"))
    job.progress(.4, tr("Installing the previous version"), force=True)
    reboot = ctypes.c_int(0)
    # use_last_error: Windows' error code is kept right after the call (GetLastError() read later can be another one)
    newdev = ctypes.WinDLL("newdev", use_last_error=True)
    newdev.UpdateDriverForPlugAndPlayDevicesW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
    ok = newdev.UpdateDriverForPlugAndPlayDevicesW(None, str(hwid), path, 1, ctypes.byref(reboot))  # INSTALLFLAG_FORCE
    err = ctypes.get_last_error() & 0xFFFFFFFF
    _cache["at"] = 0
    return {"ok": bool(ok), "reboot": bool(reboot.value), "restorePoint": rp, "msg": None if ok else tr("Windows returned error {code}.", code=f"0x{err:08X}")}


# ================= DriverStore: old copies of the same driver =================
# Every driver update leaves the previous package in C:\Windows\System32\DriverStore\FileRepository. Like
# DriverStoreExplorer's "select old drivers", a package counts as old only when a NEWER version of the same INF from
# the same provider and class is in the store. On top of that it must not be in use: no device (present OR
# disconnected) lists it in pnputil /devices, which also catches extension INFs, and Win32_PnPSignedDriver does
# not name it. Removal uses "pnputil /delete-driver" WITHOUT /force, so Windows itself refuses anything in use.
FILE_REPO = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "DriverStore", "FileRepository")
_store = {"data": None, "at": 0}


def _pnputil_xml(args, timeout=120):
    """Runs pnputil with /format xml (Windows 11 22H2+; language-independent). None if not supported."""
    import xml.etree.ElementTree as ET
    path = os.path.join(os.environ.get("TEMP", DATA), f"sh_pnp_{os.getpid()}_{time.time_ns()}.xml")
    try:
        subprocess.run(["pnputil", *args, "/format", "xml", "/output-file", path], capture_output=True, timeout=timeout,
                       creationflags=NO_WINDOW)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return None
        return ET.parse(path).getroot()
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _drv_ver_key(s):
    """'MM/DD/YYYY 1.2.3.4' -> ((1, 2, 3, 4), (YYYY, MM, DD)) for sorting."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+([\d.]+)", s or "")
    if not m:
        return ((), ())
    ver = tuple(int(x) for x in m.group(4).split(".") if x.isdigit())
    return (ver, (int(m.group(3)), int(m.group(1)), int(m.group(2))))


def _package_folder(oem, original):
    """FileRepository folder of a published package: the one whose copy of the INF is identical to INF\\oemN.inf."""
    try:
        with open(os.path.join(INF_DIR, oem), "rb") as fh:
            ref = fh.read()
    except OSError:
        return None
    prefix = original.lower() + "_"
    try:
        names = [n for n in os.listdir(FILE_REPO) if n.lower().startswith(prefix)]
    except OSError:
        return None
    for n in names:
        p = os.path.join(FILE_REPO, n, original)
        try:
            if os.path.getsize(p) == len(ref):
                with open(p, "rb") as fh:
                    if fh.read() == ref:
                        return os.path.join(FILE_REPO, n)
        except OSError:
            continue
    return None


def store_packages(fresh=False):
    """Read-only. {"supported", "packages": third-party packages in the store, "old": [...], "mb": size of the old ones}."""
    if not fresh and _store["data"] and time.time() - _store["at"] < 60:
        return _store["data"]
    root = _pnputil_xml(["/enum-drivers", "/devices"])
    if root is None:
        return {"supported": False, "packages": 0, "old": [], "mb": 0}
    try:
        current = {(d.get("inf") or "").lower() for d in list_drivers(fresh=fresh)}
    except Exception:
        current = None
    if not current:
        # without the inventory usage cannot be double-checked: offer nothing rather than guess
        return {"supported": True, "packages": len(root.findall("Driver")), "old": [], "mb": 0,
                "msg": tr("The driver inventory could not be read.")}
    groups = {}
    for d in root.findall("Driver"):
        inf = (d.get("DriverName") or "").lower()
        if not re.fullmatch(r"oem\d+\.inf", inf):
            continue
        item = {"inf": inf, "name": d.findtext("OriginalName") or "", "provider": d.findtext("ProviderName") or "",
                "cls": d.findtext("ClassName") or "", "raw": d.findtext("DriverVersion") or "",
                "devices": [(x.findtext("DeviceDescription") or "", x.findtext("Status") or "") for x in d.findall("Devices/Device")]}
        m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\S+)", item["raw"])
        item["date"] = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""
        item["version"] = m.group(4) if m else item["raw"]
        groups.setdefault((item["name"].lower(), item["provider"].lower(), item["cls"].lower()), []).append(item)
    old = []
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda i: _drv_ver_key(i["raw"]), reverse=True)
        newest = items[0]
        for it in items[1:]:
            if it["devices"] or it["inf"] in current or _drv_ver_key(it["raw"]) == _drv_ver_key(newest["raw"]):
                continue  # in use, or not actually older
            folder = _package_folder(it["inf"], it["name"])
            size = _dir_stats(folder)[1] if folder else 0
            old.append({"inf": it["inf"], "name": it["name"], "provider": it["provider"], "cls": it["cls"].upper(),
                        "version": it["version"], "date": it["date"], "newer": newest["version"], "newerDate": newest["date"],
                        "mb": round(size / 1024 ** 2, 1), "folder": folder or ""})
    old.sort(key=lambda o: -o["mb"])
    data = {"supported": True, "packages": sum(len(v) for v in groups.values()), "old": old,
            "mb": round(sum(o["mb"] for o in old), 1)}
    _store.update(data=data, at=time.time())
    return data


def delete_packages(job, infs):
    """Removes old driver packages. The list is checked again here: only packages that are old and unused RIGHT NOW
    are removed, whatever the interface sends. pnputil /delete-driver without /force: Windows refuses anything in use."""
    need = _need_admin()
    if need:
        return need
    asked = [str(i).lower() for i in (infs or []) if re.fullmatch(r"oem\d+\.inf", str(i), re.I)]
    job.progress(0, tr("Checking which packages are still unused"), force=True)
    data = store_packages(fresh=True)
    allowed = {o["inf"]: o for o in data.get("old", [])}
    todo = [i for i in asked if i in allowed]
    results = []
    for k, inf in enumerate(todo):
        job.check()
        job.progress(k / max(1, len(todo)), tr("Removing {name} {version}", name=allowed[inf]["name"], version=allowed[inf]["version"]), n=k, total=len(todo))
        code, out = run(["pnputil", "/delete-driver", inf], encoding="oem", job=job, timeout=300)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        results.append({"inf": inf, "ok": code == 0, "msg": "" if code == 0 else (lines[-1] if lines else tr("pnputil exit code {code}", code=code))})
    job.progress(1, tr("Done"), n=len(todo), total=len(todo), force=True)
    _store.update(data=None, at=0)
    freed = sum(allowed[r["inf"]]["mb"] for r in results if r["ok"])
    return {"ok": any(r["ok"] for r in results) or not todo, "results": results,
            "skipped": [i for i in asked if i not in allowed], "freedMb": round(freed, 1)}

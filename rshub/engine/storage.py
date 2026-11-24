"""Real Storage Checker: scan of a drive or folder (like WinDirStat), map, types, largest, duplicates,
comparison with the previous scan, show in Explorer and send to the Recycle Bin (with restore).

The full tree stays here (in memory); the UI only gets aggregated levels through view(id, depth)
and subtrees are requested when zooming. Each folder's small files are grouped into a single
"small files" block so the tree can hold millions of files without using hundreds of MB.
"""
import ctypes
import hashlib
import heapq
import json
import os
import string
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from ..i18n import tr

EXPORTS = {"drives", "pick_folder", "view", "info", "published", "last_scan", "types", "largest", "compare", "reveal", "trash",
           "restore", "can_delete"}
JOBS = {"scan", "duplicates"}

FILE_MIN = 1024 * 1024         # smaller files are grouped per folder (saves memory with millions of them)
BIG_KEEP = 400                 # the N largest files of the whole scan
NO_WINDOW = 0x08000000
REPARSE = 0x400
# The snapshots of the last scans (Compare, "Last scan" on the Map). Per user, in the profile: in ProgramData a scan
# run as administrator (or by an elevated routine) left a file the same user's normal runs could no longer replace,
# so "Last scan" and Compare stopped moving. The old folder is still read once per target (_snap_file).
DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
                    "RsHub", "storage")
_LEGACY = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "RsHub", "storage")

# ---------------- types (same keys as the UI) ----------------
EXT_TYPE = {}
for t, exts in {
    "game": "vpk pak bin bank pck upk uasset ubulk forge bundle assets resource rpf arc",
    "exe": "exe dll sys mui pyd ocx drv efi so node",
    "video": "mp4 mkv mov avi wmv webm m4v flv ts mts",
    "vm": "vdi vhd vhdx vmdk iso img qcow2 wim esd",
    "audio": "wav flac mp3 ogg m4a aac aif aiff ncw nki nkx nicnt mid wma opus",
    "ai": "gguf safetensors ckpt pt pth onnx bin.gguf",
    "cache": "tmp cache db dat log ldb etl evtx temp jrs chk old bak dmp lock",
    "plugin": "vst3 clap aax component vst",
    "inst": "msi cab msu msp msix appx appxbundle msixbundle",
    "archive": "zip 7z rar tar gz xz bz2 zst tgz",
    "image": "jpg jpeg png psd gif webp bmp tif tiff heic raw cr2 nef svg ico",
    "doc": "pdf docx doc xlsx xls pptx ppt md txt rtf odt csv epub",
    "code": "py js ts json pyc manifest html css cpp h c cs java go rs lua xml yml yaml ini cfg toml map jar class",
}.items():
    for e in exts.split():
        EXT_TYPE[e] = t
SYS_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp"}


def ftype(name, ext):
    if name.lower() in SYS_NAMES:
        return "system"
    return EXT_TYPE.get(ext, "cache" if not ext else "other")


# ---------------- in-memory structure ----------------
_ids = [0]


def _nid():
    _ids[0] += 1
    return _ids[0]


class D:
    __slots__ = ("id", "name", "parent", "kids", "files", "small", "size", "fc", "dirs", "mtime", "cats", "err")

    def __init__(self, name, parent):
        self.id = _nid()
        self.name = name
        self.parent = parent
        self.kids = []
        self.files = []          # [id, name, size, mtime, ext] of files >= FILE_MIN
        self.small = None        # [size, count, {type: size}] of the small ones
        self.size = 0
        self.fc = 0
        self.dirs = 0
        self.mtime = 0
        self.cats = None
        self.err = False


class Scan:
    def __init__(self, target):
        self.target = target
        self.root = None
        self.index = {}          # id -> D
        self.fidx = {}           # file id -> (D, entry)
        self.big = []            # heap (size, id)
        self.exts = {}           # ext -> [size, count, type]
        self.files = 0
        self.bytes = 0
        self.errors = 0
        self.when = 0
        self.secs = 0
        self.disk = None
        self.method = "folders"  # "mft" (file table read directly) or "folders" (folder walker)
        self.alloc = 0           # bytes actually used on disk (only measured by the MFT read)


_cur = {"scan": None}
_lock = threading.Lock()         # guards the published scan (_cur)
_scan_lock = threading.Lock()    # one scan at a time: the folder ids and the memory use are shared


def _path(d):
    parts = []
    while d is not None:
        parts.append(d.name)
        d = d.parent
    parts.reverse()
    p = parts[0]
    if p.endswith(":"):
        p += "\\"          # "C:" + "Users" has to give "C:\Users", not "C:Users"
    for x in parts[1:]:
        p = os.path.join(p, x)
    return p


# ---------------- drives and folder ----------------
def drives():
    out = []
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        kind = k32.GetDriveTypeW(root)
        if kind not in (2, 3):      # removable or fixed
            continue
        free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if not k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free)):
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        out.append({"root": root, "letter": f"{letter}:", "label": label.value, "fs": fs.value, "total": total.value,
                    "free": free.value, "used": total.value - free.value, "removable": kind == 2,
                    "system": root.lower() == (os.environ.get("SystemDrive", "C:") + "\\").lower()})
    return out


def pick_folder():
    """Windows dialog to pick a folder (on top of the app)."""
    title = tr("Choose the folder you want to scan").replace("'", "''")   # inside a single-quoted PowerShell string
    ps = ("Add-Type -AssemblyName System.Windows.Forms; $o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false};"
          f"$f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='{title}';"
          "$f.UseDescriptionForTitle=$true; if($f.ShowDialog($o) -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8; $f.SelectedPath}")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", ps], capture_output=True, creationflags=NO_WINDOW)
    p = r.stdout.decode("utf-8", "replace").strip()
    return p if p and os.path.isdir(p) else None


def _disk_of(target):
    root = os.path.splitdrive(os.path.abspath(target))[0] + "\\"
    free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if ctypes.windll.kernel32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free)):
        return {"root": root, "total": total.value, "free": free.value, "used": total.value - free.value}
    return {"root": root, "total": 0, "free": 0, "used": 0}


# ---------------- scan ----------------
FA_CLOUD = 0x1000 | 0x40000 | 0x400000   # offline / recall on open / recall on data access: OneDrive placeholders
FA_DIR = 0x10
LINK_TAGS = (0xA0000003, 0xA000000C)   # junction / mount point, symbolic link


def scan(job, target="C:\\", threads=8):
    """Scan a whole drive or a folder and publish it: the Storage Checker views show it from now on.

    The previous scan stays published until this one is complete (then they are swapped), so a scan that is still
    running, fails or is cancelled never leaves the views without data."""
    s = scan_private(job, target, threads)
    with _lock:
        _cur["scan"] = s
    return {"root": view(s.root.id, 3), "info": info()}


def scan_private(job, target="C:\\", threads=8):
    """The same scan, returned instead of published (Autopilot uses it: it must not replace the scan the UI shows).
    Scans run one at a time.

    Details of the scan:

    Whole NTFS drives with administrator rights: the MFT is read directly (ntfs.py), like WizTree; anything
    else (folders, no admin, FAT/exFAT, network) uses the parallel folder walker. Sizes are logical file sizes
    (what Explorer shows as "Size"); the MFT mode also measures the space actually used on disk."""
    target = os.path.abspath(target)
    if len(target) == 2 and target[1] == ":":
        target += "\\"
    if not os.path.isdir(target):
        raise ValueError(tr("The folder {target} doesn't exist", target=target))
    while not _scan_lock.acquire(timeout=.5):     # another scan is running: wait (a cancel still works)
        if getattr(job, "cancelled", False):
            job.stop()
    try:
        return _scan_locked(job, target, threads)
    finally:
        _scan_lock.release()


def _scan_locked(job, target, threads):
    s = Scan(target)
    s.disk = _disk_of(target)
    whole_drive = os.path.normcase(target) == os.path.normcase(s.disk["root"])
    root = D(target.rstrip("\\") if not whole_drive else target[:2], None)
    s.root = root
    s.index[root.id] = root
    t0 = time.monotonic()
    s.method = "folders"
    if whole_drive and ctypes.windll.shell32.IsUserAnAdmin():
        try:
            if _scan_mft(job, s, t0):
                s.method = "mft"
        except Exception as e:   # anything unexpected: the folder walker still gives a correct result
            if getattr(job, "cancelled", False):
                raise
            print("[storage] MFT read not used:", e)
            s.index = {root.id: root}
            root.kids, root.files, root.small, root.size, root.fc = [], [], None, 0, 0
            s.files = s.bytes = s.alloc = 0
            s.exts, s.big, s.fidx = {}, [], {}
    if s.method != "mft":
        _scan_folders(job, s, target, whole_drive, threads, t0)
    job.progress(.995, tr("Sorting…"), force=True, files=s.files, bytes=s.bytes, method=s.method)
    _finish(s)
    s.secs = round(time.monotonic() - t0, 1)
    s.when = time.time()
    _save_snapshot(s)
    return s


def _scan_mft(job, s, t0):
    """Fill the tree from the MFT. Returns False if the MFT can't be used here (then the walker runs)."""
    from . import ntfs
    expect = s.disk["used"]

    def prog(frac, recs):
        el = time.monotonic() - t0
        job.progress(min(.9, frac * .9), tr("Reading the file table"), files=recs, bytes=int(expect * frac),
                     rate=round(recs / max(.1, el)), secs=round(el, 1), exact=True, method="mft", unit="entries")

    try:
        m = ntfs.read(s.target[:2], progress=prog, cancelled=lambda: job.cancelled)
    except ntfs.NotAvailable as e:
        print("[storage] MFT not available:", e)
        return False
    if m is None:
        job.stop()
    job.progress(.92, tr("Building the folder tree"), force=True, method="mft", unit="entries")
    n, fl, par, size, alloc, mt, ex = m["n"], m["flags"], m["parent"], m["size"], m["alloc"], m["mtime"], m["ext"]
    names, exts = m["names"], m["exts"]
    used_named_dir = ntfs.F_USED | ntfs.F_NAMED | ntfs.F_DIR
    root = s.root
    dmap = {ntfs.ROOT: root}
    for r, nm in names.items():                       # every folder kept its name
        f = fl[r]
        if f & used_named_dir == used_named_dir and r != ntfs.ROOT and not ntfs.is_link(f):
            dmap[r] = D(nm, None)
    for r, d in dmap.items():
        if r == ntfs.ROOT:
            continue
        p = dmap.get(par[r])
        if p is not None and p is not d:
            d.parent = p
            p.kids.append(d)
    # only folders reachable from the root count (orphans of a live volume are left out)
    stack = [root]
    reach = set()
    while stack:
        d = stack.pop()
        if d in reach:
            continue
        reach.add(d)
        s.index[d.id] = d
        stack.extend(d.kids)
    if len(reach) != len(dmap):
        dmap = {k: v for k, v in dmap.items() if v in reach}
    ext_type = [EXT_TYPE.get(e, "cache" if not e else "other") for e in exts]
    ext_acc = [[0, 0] for _ in exts]
    big = []
    files = nbytes = nalloc = 0
    used_named = ntfs.F_USED | ntfs.F_NAMED
    skip = ntfs.F_DIR | ntfs.F_CLOUD
    get_dir = dmap.get
    get_name = names.get
    for r in range(n):
        f = fl[r]
        if f & used_named != used_named or f & skip:
            continue
        d = get_dir(par[r])
        if d is None:
            continue
        sz = size[r]
        xi = ex[r]
        files += 1
        nbytes += sz
        nalloc += alloc[r]
        acc = ext_acc[xi]
        acc[0] += sz
        acc[1] += 1
        tm = mt[r]
        if tm > d.mtime:
            d.mtime = tm
        d.fc += 1
        d.size += sz
        nm = get_name(r)
        ty = "system" if nm is not None and (nm[0] == "$" or nm.lower() in SYS_NAMES) else ext_type[xi]
        if sz >= FILE_MIN:
            fid = _nid()
            ent = [fid, nm or f"file {r}", sz, tm, exts[xi]]
            d.files.append(ent)
            s.fidx[fid] = (d, ent)
            if len(big) < BIG_KEEP:
                heapq.heappush(big, (sz, fid))
            elif sz > big[0][0]:
                heapq.heapreplace(big, (sz, fid))
        else:
            sm = d.small
            if sm is None:
                sm = d.small = [0, 0, {}]
            sm[0] += sz
            sm[1] += 1
            sm[2][ty] = sm[2].get(ty, 0) + sz
        if r & 0x3FFFF == 0:
            if job.cancelled:
                job.stop()
            job.progress(.92 + .06 * r / n, tr("Building the folder tree"), files=files, method="mft", unit="entries")
    for i, (b, c) in enumerate(ext_acc):
        if c:
            s.exts[exts[i]] = [b, c, ext_type[i]]
    s.big = big
    s.files, s.bytes, s.alloc = files, nbytes, nalloc
    return True


def _scan_folders(job, s, target, whole_drive, threads, t0):
    """Parallel folder walker (os.scandir: on Windows the listing already brings size, dates and attributes).
    Each thread keeps its own counters and merges them once at the end: no lock per file."""
    expect = s.disk["used"] if whole_drive else 0
    root = s.root
    work = [(root, target)]
    cond = threading.Condition()
    state = {"cur": target, "pending": 1, "files": 0, "bytes": 0}
    done = threading.Event()
    results = []

    def worker():
        exts_l, big_l, fidx_l = {}, [], {}
        errors_t = 0
        while True:
            with cond:
                while not work and state["pending"] and not done.is_set():
                    cond.wait(0.2)
                if not work or done.is_set():
                    break
                d, p = work.pop()
            files = []
            small = None
            nf = nb = 0
            mt = 0
            kids = []
            try:
                with os.scandir(p) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        attrs = st.st_file_attributes
                        if attrs & FA_DIR:
                            # junctions and symbolic links are not followed (nothing is counted twice); other
                            # reparse folders (OneDrive) hold real files and are walked
                            if not attrs & REPARSE or st.st_reparse_tag not in LINK_TAGS:
                                kids.append(e.name)
                            continue
                        if attrs & FA_CLOUD:
                            continue                  # OneDrive placeholder: not stored on this disk
                        size = st.st_size
                        nf += 1
                        nb += size
                        m = st.st_mtime
                        if m > mt:
                            mt = m
                        name = e.name
                        dot = name.rfind(".")
                        ext = name[dot + 1:].lower() if dot > 0 else ""
                        ex = exts_l.get(ext)
                        if ex is None:
                            exts_l[ext] = [size, 1]
                        else:
                            ex[0] += size
                            ex[1] += 1
                        if size >= FILE_MIN:
                            fid = _nid()
                            ent = [fid, name, size, m, ext]
                            files.append(ent)
                            fidx_l[fid] = (d, ent)
                            if len(big_l) < BIG_KEEP:
                                heapq.heappush(big_l, (size, fid))
                            elif size > big_l[0][0]:
                                heapq.heapreplace(big_l, (size, fid))
                        else:
                            ty = ftype(name, ext)
                            if small is None:
                                small = [0, 0, {}]
                            small[0] += size
                            small[1] += 1
                            small[2][ty] = small[2].get(ty, 0) + size
            except OSError:
                d.err = True
                errors_t += 1
            d.files = files
            d.small = small
            d.mtime = mt
            d.size = nb
            d.fc = nf
            subs = [D(name, d) for name in kids]
            d.kids = subs
            with cond:
                for c in subs:
                    s.index[c.id] = c
                state["files"] += nf
                state["bytes"] += nb
                state["cur"] = p
                state["pending"] += len(subs) - 1
                work.extend((c, os.path.join(p, c.name)) for c in subs)
                if state["pending"] == 0:
                    done.set()
                cond.notify_all()
        results.append((exts_l, big_l, fidx_l, errors_t))

    ws = [threading.Thread(target=worker, daemon=True, name=f"scan-{i}") for i in range(max(2, threads))]
    for w in ws:
        w.start()
    while not done.wait(0.12):
        if job.cancelled:
            done.set()
            with cond:
                cond.notify_all()
            job.stop()
        el = time.monotonic() - t0
        frac = min(0.99, state["bytes"] / expect) if expect else None
        job.progress(frac if frac is not None else min(0.95, 1 - 1 / (1 + el / 20)), state["cur"],
                     files=state["files"], bytes=state["bytes"], rate=round(state["files"] / max(.1, el)),
                     secs=round(el, 1), exact=bool(expect), method="folders", unit="files")
    with cond:
        cond.notify_all()
    for w in ws:
        w.join(timeout=2)
    big = []
    for exts_l, big_l, fidx_l, err in results:     # merge what each thread collected
        for e, (b, c) in exts_l.items():
            x = s.exts.get(e)
            if x is None:
                s.exts[e] = [b, c, ftype("", e)]
            else:
                x[0] += b
                x[1] += c
        big.extend(big_l)
        s.fidx.update(fidx_l)
        s.errors += err
    s.big = heapq.nlargest(BIG_KEEP, big)
    heapq.heapify(s.big)
    s.files, s.bytes = state["files"], state["bytes"]


def _finish(s):
    """Sizes, counts and types from the bottom up (no recursion: some trees are very deep)."""
    order = []
    stack = [s.root]
    while stack:
        d = stack.pop()
        order.append(d)
        stack.extend(d.kids)
    for d in reversed(order):
        cats = {}
        for f in d.files:
            ty = ftype(f[1], f[4])
            cats[ty] = cats.get(ty, 0) + f[2]
        if d.small:
            for ty, b in d.small[2].items():
                cats[ty] = cats.get(ty, 0) + b
        for c in d.kids:
            d.size += c.size
            d.fc += c.fc
            d.dirs += 1 + c.dirs
            if c.mtime > d.mtime:
                d.mtime = c.mtime
            for ty, b in c.cats.items():
                cats[ty] = cats.get(ty, 0) + b
        d.cats = cats
        d.kids.sort(key=lambda x: x.size, reverse=True)
        d.files.sort(key=lambda x: x[2], reverse=True)


def _get():
    s = _cur["scan"]
    if s is None:
        raise RuntimeError(tr("There is no scan yet"))
    return s


def _dom(cats):
    return max(cats.items(), key=lambda kv: kv[1])[0] if cats else "other"


def _node(d, depth, min_bytes, max_kids):
    """A folder for the UI, with its children down to `depth` levels; anything too small is grouped."""
    out = {"id": d.id, "name": d.name, "dir": True, "size": d.size, "fc": d.fc, "dirs": d.dirs, "mtime": int(d.mtime * 1000),
           "cats": d.cats, "dom": _dom(d.cats), "err": d.err}
    if depth <= 0:
        out["lazy"] = bool(d.kids or d.files or d.small)
        out["kids"] = []
        return out
    items = []
    for c in d.kids:
        items.append((c.size, "d", c))
    for f in d.files:
        items.append((f[2], "f", f))
    if d.small:
        items.append((d.small[0], "s", d.small))
    items.sort(key=lambda x: x[0], reverse=True)
    kids, rest, rest_n, rest_cats = [], 0, 0, {}
    for size, kind, x in items:
        if size <= 0:
            continue
        if len(kids) >= max_kids or (size < min_bytes and len(kids) >= 3):
            rest += size
            if kind == "d":
                rest_n += x.fc
                for ty, b in x.cats.items():
                    rest_cats[ty] = rest_cats.get(ty, 0) + b
            elif kind == "f":
                rest_n += 1
                ty = ftype(x[1], x[4])
                rest_cats[ty] = rest_cats.get(ty, 0) + x[2]
            else:
                rest_n += x[1]
                for ty, b in x[2].items():
                    rest_cats[ty] = rest_cats.get(ty, 0) + b
            continue
        if kind == "d":
            kids.append(_node(x, depth - 1, min_bytes, max_kids))
        elif kind == "f":
            kids.append({"id": x[0], "name": x[1], "size": x[2], "mtime": int(x[3] * 1000), "ext": x[4], "type": ftype(x[1], x[4])})
        else:
            cats = x[2]
            kids.append({"id": -d.id, "name": tr("{n} small files", n=x[1]) if x[1] != 1 else tr("1 small file"),
                         "size": x[0], "fc": x[1], "ext": "", "type": _dom(cats), "group": True, "mtime": int(d.mtime * 1000)})
    if rest > 0:
        kids.append({"id": -(10_000_000 + d.id), "name": tr("Other smaller items"), "size": rest, "fc": rest_n,
                     "ext": "", "type": _dom(rest_cats), "group": True, "mtime": int(d.mtime * 1000)})
    out["kids"] = kids
    return out


def view(node_id=None, depth=3, max_kids=70):
    """The node with its descendants down to `depth` levels (what the map or the tree draws)."""
    s = _get()
    d = s.index.get(int(node_id)) if node_id else s.root
    if d is None:
        raise KeyError(tr("That folder is no longer in the scan"))
    min_bytes = max(1, int(d.size * 0.0012))
    return _node(d, int(depth), min_bytes, int(max_kids))


def info():
    s = _get()
    return {"target": s.target, "files": s.files, "bytes": s.bytes, "dirs": s.root.dirs, "errors": s.errors,
            "when": int(s.when * 1000), "secs": s.secs, "disk": s.disk, "rootId": s.root.id,
            "admin": bool(ctypes.windll.shell32.IsUserAnAdmin()), "method": s.method,
            "alloc": s.alloc or None,           # space used on disk (MFT read only)
            "hardlinks": "once" if s.method == "mft" else "each"}


def types():
    s = _get()
    by = {}
    for ext, (size, n, ty) in s.exts.items():
        b = by.setdefault(ty, {"type": ty, "size": 0, "count": 0, "exts": []})
        b["size"] += size
        b["count"] += n
        b["exts"].append([ext or tr("(no extension)"), size])
    out = sorted(by.values(), key=lambda b: b["size"], reverse=True)
    for b in out:
        b["exts"] = sorted(b["exts"], key=lambda x: x[1], reverse=True)[:6]
    return out


def largest(n=200):
    s = _get()
    out = []
    for size, fid in sorted(s.big, reverse=True)[:int(n)]:
        d, ent = s.fidx.get(fid, (None, None))
        if d is None:
            continue
        out.append({"id": fid, "name": ent[1], "size": ent[2], "mtime": int(ent[3] * 1000), "ext": ent[4],
                    "type": ftype(ent[1], ent[4]), "dir": _path(d), "dirId": d.id})
    return out


# ---------------- duplicates ----------------
SKIP_DUP = ("\\windows\\", "\\$recycle.bin\\", "\\system volume information\\", "\\windowsapps\\", "\\winsxs\\")
# Programs and games: identical DLLs/assets live in several of these on purpose; deleting "copies" breaks them.
SKIP_PROGRAMS = ("\\program files\\", "\\program files (x86)\\", "\\programdata\\", "\\appdata\\",
                 "\\steamapps\\", "\\steamlibrary\\", "\\steam\\", "\\epic games\\", "\\gog games\\", "\\gog galaxy\\",
                 "\\xboxgames\\", "\\riot games\\", "\\ea games\\", "\\origin games\\", "\\ubisoft game launcher\\",
                 "\\battle.net\\", "\\games\\", "\\node_modules\\", "\\site-packages\\")


def _dup_skipped(path):
    """True for files in Windows folders and in program or game trees: they never show up as duplicates."""
    low = path.lower() + "\\"
    if any(k in low for k in SKIP_DUP) or any(k in low for k in SKIP_PROGRAMS):
        return True
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "APPDATA", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root and low.startswith(os.path.normcase(root).lower().rstrip("\\") + "\\"):
            return True
    return False


def duplicates(job, min_mb=1):
    """Groups by size, then by a partial hash (start + end) and finally by a full hash."""
    return _duplicates(job, min_mb, _get())


def _duplicates(job, min_mb, s):
    min_b = int(float(min_mb) * 1024 * 1024)
    by_size = {}
    for fid, (d, ent) in s.fidx.items():
        if ent[2] < min_b:
            continue
        by_size.setdefault(ent[2], []).append((d, ent))
    cands = [(sz, lst) for sz, lst in by_size.items() if len(lst) > 1]
    paths = []
    skipped = 0          # candidate files left out because they belong to programs or games
    for sz, lst in cands:
        group = []
        for d, ent in lst:
            p = os.path.join(_path(d), ent[1])
            if _dup_skipped(p):
                skipped += 1
            else:
                group.append((p, ent))
        if len(group) > 1:
            paths.append((sz, group))
    paths.sort(key=lambda x: x[0] * (len(x[1]) - 1), reverse=True)
    total = sum(sz * len(g) for sz, g in paths) or 1
    done_b = 0
    groups = []

    def part(p, sz):
        h = hashlib.blake2b(digest_size=16)
        with open(p, "rb") as fh:
            h.update(fh.read(65536))
            if sz > 131072:
                fh.seek(-65536, 2)
                h.update(fh.read(65536))
        return h.hexdigest()

    def full(p):
        h = hashlib.blake2b(digest_size=20)
        with open(p, "rb") as fh:
            while True:
                if job.cancelled:
                    job.stop()
                b = fh.read(4 * 1024 * 1024)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    for sz, group in paths:
        job.check()
        sub = {}
        for p, ent in group:
            try:
                sub.setdefault(part(p, sz), []).append((p, ent))
            except OSError:
                pass
        for lst in sub.values():
            if len(lst) < 2:
                continue
            fulls = {}
            for p, ent in lst:
                job.progress(done_b / total, p, groups=len(groups))
                try:
                    fulls.setdefault(full(p) if sz > 131072 else "x", []).append((p, ent))
                except OSError:
                    pass
                done_b += sz
            for lst2 in fulls.values():
                if len(lst2) > 1:
                    groups.append({"name": lst2[0][1][1], "size": sz, "ext": lst2[0][1][4], "type": ftype(lst2[0][1][1], lst2[0][1][4]),
                                   "copies": [{"path": p, "dir": os.path.dirname(p), "mtime": int(e[3] * 1000)} for p, e in sorted(lst2, key=lambda x: x[1][3])]})
        if len(groups) >= 300:
            break
    groups.sort(key=lambda g: g["size"] * (len(g["copies"]) - 1), reverse=True)
    return {"groups": groups[:200], "wasted": sum(g["size"] * (len(g["copies"]) - 1) for g in groups),
            "skippedPrograms": skipped}


# ---------------- compare with the previous scan ----------------
def _old_key(target):
    return "".join(ch if ch.isalnum() else "_" for ch in target.lower()).strip("_") or "root"


def _key(target):
    """The snapshot's file name: readable, plus a hash of the whole path ("C:\\a b", "C:\\a-b" and "C:\\a_b" used to
    share one file, so one folder's Last scan and Compare showed another's)."""
    tag = hashlib.sha1(os.path.normcase(os.path.abspath(target)).encode("utf-8", "surrogatepass")).hexdigest()[:10]
    return f"{_old_key(target)[:60]}-{tag}"


def _same_target(a, b):
    try:
        return os.path.normcase(os.path.abspath(a or "")) == os.path.normcase(os.path.abspath(b or ""))
    except (TypeError, ValueError):
        return False


def _read_snap(f):
    """A snapshot file as a dict, or None when it is missing or damaged (a crash while it was written)."""
    try:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _snap_file(target):
    """Where the snapshot of `target` lives. The first time, one kept by an earlier version (old name, or the old
    ProgramData folder) is brought over if it really is this target's."""
    f = os.path.join(DATA, _key(target) + ".json")
    if os.path.exists(f):
        return f
    for d in (DATA, _LEGACY):
        old = os.path.join(d, _old_key(target) + ".json")
        data = _read_snap(old)
        if data and _same_target(((data.get("last") or {}).get("target")), target):
            try:
                os.makedirs(DATA, exist_ok=True)
                _write_snap(f, data)
            except OSError:
                return old
            break
    return f


def _write_snap(f, data):
    with open(f + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(f + ".tmp", f)


def _save_snapshot(s):
    """Per-folder summary (the heavy ones) + large files, to compare next time."""
    try:
        os.makedirs(DATA, exist_ok=True)
        dirs = {}
        stack = [(s.root, 0)]
        floor = max(50 * 1024 * 1024, s.root.size * 0.0005)
        while stack:
            d, depth = stack.pop()
            if d.size < floor or depth > 7:
                continue
            dirs[_path(d)] = [d.size, d.fc]
            stack.extend((c, depth + 1) for c in d.kids)
        bigs = []
        for size, fid in s.big:
            if size >= 300 * 1024 * 1024:
                d, ent = s.fidx[fid]
                bigs.append([os.path.join(_path(d), ent[1]), size, int(ent[3])])
        snap = {"v": 2, "target": s.target, "when": s.when, "size": s.root.size, "files": s.files, "dirs": dirs, "big": bigs}
        f = _snap_file(s.target)
        old = _read_snap(f) or {}  # damaged: start again (it used to stop every later snapshot of this target)
        prev = old.get("last")
        hist = old.get("history") if isinstance(old.get("history"), list) else []
        hist = (hist + [[s.when, s.root.size]])[-24:]
        os.makedirs(os.path.dirname(f), exist_ok=True)
        _write_snap(f, {"last": snap, "prev": prev, "history": hist})
    except Exception as e:  # comparing is an extra: it never breaks the scan
        print("storage snapshot:", e)


def published():
    """The scan the Storage Checker shows, if this process already has one (the page was reloaded, e.g. after a
    language change): its root and info, exactly what the scan job returns, or None. Nothing is read from the disk."""
    s = _cur["scan"]
    if s is None:
        return None
    return {"root": view(s.root.id, 3), "info": info()}


def last_scan(target="C:\\"):
    """When the last scan of `target` ran and what it found (the snapshot kept for Compare, also written by Autopilot's
    private scans), or None. A small file read: opening the Storage Checker shows it instead of scanning again."""
    try:
        if len(target) == 2 and target[1] == ":":      # "C:" alone would be the current folder of that drive
            target += "\\"
        target = os.path.abspath(target)
        last = (_read_snap(_snap_file(target)) or {}).get("last") or {}
        if not last.get("when") or not _same_target(last.get("target") or target, target):
            return None
        return {"target": last.get("target") or target, "when": int(last["when"] * 1000), "size": last.get("size") or 0,
                "files": last.get("files") or 0}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def compare():
    return _compare(_get())


def _compare(s):
    data = _read_snap(_snap_file(s.target))
    if not data or not isinstance(data.get("last"), dict):
        return {"prev": None}
    cur, prev = data.get("last"), data.get("prev")
    hist = data.get("history", [])
    if not prev or prev.get("v") != 2:   # snapshots in an older format can't be compared
        return {"prev": None, "history": hist}
    a, b = prev["dirs"], cur["dirs"]
    changes = []
    root = os.path.normcase(cur["target"].rstrip("\\"))
    for p in set(a) | set(b):
        if os.path.normcase(p.rstrip("\\")) == root:
            continue                     # the root is the total: it's already in the header
        sa, sb = a.get(p, [0, 0]), b.get(p, [0, 0])
        dd = sb[0] - sa[0]
        if abs(dd) >= 64 * 1024 * 1024:
            changes.append([p, dd, sb[1] - sa[1]])
    changes.sort(key=lambda c: abs(c[1]), reverse=True)
    picked = []
    for p, dd, df in changes:
        pl = p.lower().rstrip("\\") + "\\"
        # if a subfolder already explains almost all of the change, the parent folder is redundant
        if any(q.lower().startswith(pl) and abs(qd) >= abs(dd) * .8 for q, qd, _ in picked):
            continue
        picked = [x for x in picked if not (pl.startswith(x[0].lower().rstrip("\\") + "\\") and abs(dd) >= abs(x[1]) * .8)] + [[p, dd, df]]
        if len(picked) >= 14:
            break
    picked.sort(key=lambda c: abs(c[1]), reverse=True)
    newbig = [x for x in cur.get("big", []) if x[2] > prev["when"]]
    newbig.sort(key=lambda x: x[1], reverse=True)
    return {"prev": prev["when"] * 1000, "now": cur["when"] * 1000, "delta": cur["size"] - prev["size"],
            "dfiles": cur["files"] - prev["files"], "changes": picked, "newbig": newbig[:12], "history": hist}


# ---------------- file actions ----------------
def _drive_roots():
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []
    return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]


def _protected_extra(path):
    """The shared safety rules of the Program Remover (removal/safety.py) plus what only matters here: other users'
    profiles, program folders on every drive, and the recycle bin / restore data of every drive. The path is judged
    under every name it has (as written and where it really is), so a junction, a short name, another case or a
    trailing dot cannot hide a protected folder."""
    from .removal import paths as rp, safety
    forms = rp.forms(path)
    if not forms:
        return tr("It's inside a folder RsHub never touches.")
    protected = safety.protected_exact() | rp.self_paths()       # the folder itself or one that contains it
    allowed = safety.forbidden_exceptions()
    forbidden = safety.forbidden_trees()
    k = rp.known_folders()
    prog = {f for x in (k.get("ProgramFiles"), k.get("ProgramFilesX86"), os.environ.get("ProgramW6432"),
                        os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if x
            for f in rp.forms_cached(x)}
    for root in _drive_roots():
        for x in (os.path.join(root, "Program Files"), os.path.join(root, "Program Files (x86)")):
            prog.update(rp.forms_cached(x))
    win = rp.forms_cached(k.get("Windows") or "")
    prof = rp.forms_cached(k.get("Profile") or os.path.expanduser("~"))
    users = {os.path.dirname(f) for f in prof}
    for n in forms:
        parts = n.split("\\")
        if len(parts) >= 2 and parts[1] in ("$recycle.bin", "system volume information"):
            return tr("It's inside a folder RsHub never touches.")
        for pe in protected:
            if rp.within_norm(pe, n):
                return tr("It's one of the main folders of your profile.")
        if not any(rp.within_norm(n, ex) for ex in allowed):
            for ft in forbidden:
                if rp.within_norm(n, ft):
                    return tr("It's inside a folder RsHub never touches.")
        if any(rp.within_norm(n, x) for x in prog):
            return tr("It's a program folder: uninstall it from Program Remover.")
        if any(rp.within_norm(n, w) for w in win):
            return tr("It's inside a folder RsHub never touches.")
        if any(rp.within_norm(n, u) for u in users if u) and not any(rp.within_norm(n, pf) for pf in prof):
            return tr("It belongs to another user.")
    return None


def _protected(path):
    """Why `path` must not be deleted, or None. Every comparison is on real paths (see _protected_extra)."""
    try:
        from .removal import paths as rp
        forms = rp.forms(os.path.abspath(path))
        if not forms:
            return tr("It's inside a folder RsHub never touches.")
        roots = [os.environ.get("SystemRoot", r"C:\Windows"), r"C:\$Recycle.Bin", r"C:\System Volume Information"]
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        roots += [os.path.join(pd, "Microsoft"), os.path.join(pd, "RsHub"),
                  str(Path(os.path.abspath(__file__)).parents[2])]
        if getattr(sys, "frozen", False):  # installed: the folder that holds RsHub.exe
            roots.append(os.path.dirname(sys.executable))
        roots = {f for r in roots for f in rp.forms_cached(r)}
        pfs = {f for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                            os.environ.get("ProgramW6432")) if pf for f in rp.forms_cached(pf)}
        for p in forms:
            if any(rp.within_norm(p, r) for r in roots):
                return tr("It's inside a folder RsHub never touches.")
            if any(p == pf or os.path.dirname(p) == pf for pf in pfs):
                return tr("It's a program folder: uninstall it from Program Remover.")
            if os.path.basename(p) in SYS_NAMES:
                return tr("It's a Windows file (virtual memory or hibernation).")
            if os.path.splitdrive(p)[1] in ("", "\\"):
                return tr("It's the root of a drive.")
        return _protected_extra(path)
    except Exception as e:      # when the shared rules can't be read, block: a leftover is better than a lost file
        print("[storage] safety rules unavailable:", e)
        return tr("It's inside a folder RsHub never touches.")


def can_delete(path):
    why = _protected(path)
    return {"ok": why is None, "why": why}


def reveal(path):
    if os.path.exists(path):
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)], creationflags=NO_WINDOW)
        return True
    d = os.path.dirname(path)
    if os.path.isdir(d):
        os.startfile(d)
        return True
    return False


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


def _size_up_to(path, limit):
    """Size in bytes of a file or folder, stopping as soon as it is above `limit` (links are not followed)."""
    try:
        if os.path.isfile(path) or os.path.islink(path):
            return os.lstat(path).st_size
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if total > limit:
                        return total
        except OSError:
            continue
    return total


BIN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"


def _bin_settings(root):
    """(Recycle Bin disabled for this drive?, size limit in bytes or None = Windows default). Reads what the
    "Recycle Bin Properties" dialog saves: per volume first, then the "same settings for all drives" ones."""
    import winreg
    guid = None
    buf = ctypes.create_unicode_buffer(80)
    if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, 80):
        a, b = buf.value.find("{"), buf.value.find("}")
        if 0 <= a < b:
            guid = buf.value[a:b + 1].upper()

    def val(sub, name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BIN_KEY + sub) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None
    per = "\\Volume\\" + guid if guid else None
    nuke = (val(per, "NukeOnDelete") if per else None)
    if nuke is None:
        nuke = val("", "NukeOnDelete")
    cap = (val(per, "MaxCapacity") if per else None)
    if cap is None:
        cap = val("", "MaxCapacity")
    return bool(nuke), (int(cap) * 1024 * 1024 if cap else None)


def _drive_type(root):
    return ctypes.windll.kernel32.GetDriveTypeW(root)


def _default_bin_cap(total):
    """Windows' default Recycle Bin size (Vista and later): 10% of the first 40 GB of the volume plus 5% of the rest.
    It used to be taken as 10% of the whole volume: on a 1 TB drive anything between ~53 GB and 100 GB was "sent to the
    Recycle Bin" and Windows, finding it too big, deleted it for good without asking."""
    first = 40 * 1024 ** 3
    return int(min(total, first) * .10 + max(0, total - first) * .05)


def can_recycle(path):
    """Would Windows really keep this in the Recycle Bin? (ok, reason). SHFileOperation deletes for good, without a
    word, on removable and network drives, when the bin is set to "don't move files to the Recycle Bin", and for
    items bigger than the bin's limit, so none of that can be left to chance."""
    path = os.path.abspath(path)
    root = os.path.splitdrive(path)[0] + "\\"
    if path.startswith("\\\\") or len(root) < 3 or _drive_type(root) != 3:   # not a fixed local disk
        return False, "drive"
    try:
        off, cap = _bin_settings(root)
    except Exception:
        off, cap = False, None
    if off:
        return False, "off"
    if cap is None:
        cap = _default_bin_cap(_disk_of(path)["total"])
    if cap <= 0 or _size_up_to(path, cap) > cap:
        return False, "size"
    return True, ""


def _shell_delete(path, undo):
    """SHFileOperation FO_DELETE: to the Recycle Bin (undo=True) or for good. True when it is gone."""
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = path + "\0"
    flags = 0x0010 | 0x0004 | 0x0400  # NOCONFIRMATION | SILENT | NOERRORUI
    op.fFlags = flags | (0x0040 if undo else 0)  # ALLOWUNDO
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return not (r != 0 or op.fAnyOperationsAborted or os.path.exists(path))


def trash(path, permanent=False):
    """Sends to the Windows Recycle Bin (can be restored). Never inside protected areas.

    If it can't really be recycled (removable or network drive, bin turned off, too big for it) nothing is deleted:
    the result has noBin=True so the UI can ask, and only permanent=True (after that question) deletes for good."""
    path = os.path.abspath(path)
    why = _protected(path)
    if why:
        return {"ok": False, "error": why}
    if not os.path.exists(path):
        return {"ok": False, "error": tr("It no longer exists.")}
    size = 0
    try:
        size = os.path.getsize(path) if os.path.isfile(path) else 0
    except OSError:
        pass
    permanent = permanent is True
    if not permanent:
        ok, _why = can_recycle(path)
        if not ok:
            return {"ok": False, "noBin": True,
                    "error": tr("It can't go to the Recycle Bin (the drive has none, or it's too big for it), so it was not deleted.")}
    if not _shell_delete(path, undo=not permanent):
        return {"ok": False, "error": tr("Windows couldn't move it to the Recycle Bin (is it open, or does it need admin permissions?).")}
    s = _cur["scan"]
    if s is not None:
        _forget(s, path)
    return {"ok": True, "size": size, "permanent": permanent}


def _forget(s, path):
    """Removes what was sent to the Recycle Bin from the scan, so the views stop showing it."""
    pl = os.path.normcase(path)
    for fid, (d, ent) in list(s.fidx.items()):
        if os.path.normcase(os.path.join(_path(d), ent[1])) == pl:
            d.files = [f for f in d.files if f[0] != fid]
            s.fidx.pop(fid, None)
            s.big = [x for x in s.big if x[1] != fid]
            heapq.heapify(s.big)
            x = d
            while x is not None:
                x.size -= ent[2]
                x.fc -= 1
                ty = ftype(ent[1], ent[4])
                if x.cats and ty in x.cats:
                    x.cats[ty] = max(0, x.cats[ty] - ent[2])
                x = x.parent
            return


def restore(path):
    """Takes the latest item deleted from that original path out of the Recycle Bin and puts it back."""
    path = os.path.abspath(path)
    drive = os.path.splitdrive(path)[0] or "C:"
    rb = drive + "\\$Recycle.Bin"
    best = None
    try:
        for sid in os.listdir(rb):
            folder = os.path.join(rb, sid)
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for n in names:
                if not n.startswith("$I"):
                    continue
                try:
                    with open(os.path.join(folder, n), "rb") as fh:
                        raw = fh.read()
                    ver = int.from_bytes(raw[0:8], "little")
                    when = int.from_bytes(raw[16:24], "little")
                    if ver == 2:
                        ln = int.from_bytes(raw[24:28], "little")
                        orig = raw[28:28 + ln * 2].decode("utf-16-le").rstrip("\0")
                    else:
                        orig = raw[24:24 + 520].decode("utf-16-le").split("\0")[0]
                except (OSError, ValueError):   # unreadable, or a truncated/odd $I file: not a match, the rest still count
                    continue
                if os.path.normcase(orig) == os.path.normcase(path) and (best is None or when > best[0]):
                    best = (when, os.path.join(folder, "$R" + n[2:]), os.path.join(folder, n))
    except OSError:
        pass
    if not best:
        return {"ok": False, "error": tr("It's not in the Recycle Bin. You can look for it in the Windows Recycle Bin.")}
    if os.path.exists(path):
        return {"ok": False, "error": tr("There's already another file with that name in its place.")}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.replace(best[1], path)
        try:
            os.remove(best[2])
        except OSError:
            pass
    except OSError:
        return {"ok": False, "error": tr("Couldn't restore it from here: open it from the Windows Recycle Bin.")}
    return {"ok": True}

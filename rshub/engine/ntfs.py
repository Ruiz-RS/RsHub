"""Read an NTFS volume's Master File Table (MFT) directly, the way WizTree does.

Walking folders asks Windows about every directory one by one (open, list, close): with millions of files
that takes minutes. The MFT already holds one record per file and folder (name, parent folder, size, dates),
so reading it sequentially from the raw volume (\\\\.\\C:) and parsing it is much faster. Parsing is the
slow part in Python, so the MFT is split into ranges that several worker processes parse in parallel.
It needs administrator rights (raw volume access) and only works on NTFS; storage.py falls back to the
folder walker otherwise. Everything here is READ-ONLY.

Format notes (as used by analyzeMFT, ntfs-core, go-ntfs and the NTFS documentation):
- Boot sector: bytes/sector @0x0B, sectors/cluster @0x0D, MFT start cluster @0x30, record size @0x40.
- Each FILE record (usually 1024 bytes) is protected by an "update sequence": the last 2 bytes of every
  sector were replaced by a check value and must be restored ("fixup") before the record is read.
- Record header: flags @0x16 (1 = in use, 2 = directory), first attribute @0x14, base record @0x20
  (non-zero for extension records, whose attributes belong to that base record).
- Attributes: 0x10 $STANDARD_INFORMATION (dates, file attributes), 0x30 $FILE_NAME (parent folder and name;
  there can be a DOS 8.3 name and several hard links), 0x80 $DATA (the unnamed one is the file content; when
  it lives outside the record, its real and allocated sizes are in the attribute header), 0x20 attribute list.
Known traps handled: torn records (fixup mismatch) are skipped; hard links are counted once (first name);
DOS 8.3 names are replaced by the long name; a file's size can sit in an extension record; the MFT itself can
be fragmented (its own data runs are followed); OneDrive placeholders (offline / recall flags) are marked.
"""
import os
import struct
import time
from array import array

U16 = struct.Struct("<H").unpack_from
U32 = struct.Struct("<I").unpack_from
U64 = struct.Struct("<Q").unpack_from
I64 = struct.Struct("<q").unpack_from
ATTR_HDR = struct.Struct("<II").unpack_from
REF_MASK = 0xFFFFFFFFFFFF            # the low 48 bits of a file reference are the record number
EPOCH = 116444736000000000           # FILETIME of 1970-01-01

F_USED, F_DIR, F_REPARSE, F_NAMED, F_CLOUD, F_NOTLINK = 1, 2, 4, 8, 16, 32
FA_REPARSE = 0x400
LINK_TAGS = (0xA0000003, 0xA000000C)      # junction / mount point, symbolic link: never followed
# A reparse point is only a "link" when its tag is one of LINK_TAGS. OneDrive folders, deduplicated files and
# app-execution aliases are reparse points too but hold real data, so they are counted (F_NOTLINK).
# offline / recall on data access: OneDrive files that are not stored on this disk. 0x40000 (recall on open in
# Win32 listings) is NOT used: on disk that bit means "has extended attributes", set on many system files.
FA_CLOUD = 0x1000 | 0x400000
ROOT = 5                                  # record number of the root folder
KEEP_NAME_BYTES = 1024 * 1024             # same threshold as storage.FILE_MIN: smaller files are grouped there
_KEEP = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp"}


class NotAvailable(Exception):
    """The MFT can't be read here (no admin rights, not NTFS, unexpected layout): use the folder walker."""


def _open(volume):
    path = "\\\\.\\" + volume.rstrip("\\")
    try:
        return open(path, "rb", buffering=0)
    except PermissionError:
        raise NotAvailable("administrator rights are needed to read the file table")
    except OSError as e:
        raise NotAvailable(str(e))


def _boot(f):
    b = bytearray(4096)     # a raw volume only reads whole sectors: 4K-native disks fail with 512 bytes
    f.seek(0)
    f.readinto(b)
    if b[3:11] != b"NTFS    ":
        raise NotAvailable("not an NTFS volume")
    sector = U16(b, 0x0B)[0]
    spc = b[0x0D]
    cluster = sector * (spc if spc <= 0x80 else 1 << (256 - spc))
    mft_lcn = I64(b, 0x30)[0]
    c = b[0x40] if b[0x40] < 0x80 else b[0x40] - 256
    rec = c * cluster if c > 0 else 1 << -c
    return sector, cluster, mft_lcn, rec


def _fixup(b, off, rec, sector):
    """Restore the last 2 bytes of each sector of the record. False if the record is torn (being written)."""
    usa = U16(b, off + 4)[0]
    n = U16(b, off + 6)[0]
    usn = b[off + usa:off + usa + 2]
    for i in range(1, n):
        p = off + i * sector - 2
        if p + 2 > off + rec:
            break
        if b[p:p + 2] != usn:
            return False
        q = off + usa + 2 * i
        b[p:p + 2] = b[q:q + 2]
    return True


def _runs(b, a):
    """Data runs of a non-resident attribute: [(first cluster or None if sparse, number of clusters)]."""
    p = a + U16(b, a + 0x20)[0]
    out, lcn = [], 0
    while True:
        h = b[p]
        if h == 0:
            break
        ln, lo = h & 0x0F, h >> 4
        length = int.from_bytes(b[p + 1:p + 1 + ln], "little")
        if lo:
            lcn += int.from_bytes(b[p + 1 + ln:p + 1 + ln + lo], "little", signed=True)
            out.append((lcn, length))
        else:
            out.append((None, length))
        p += 1 + ln + lo
    return out


def layout(volume):
    """Geometry of the volume and where the MFT lives (it can be fragmented): a picklable dict."""
    with _open(volume) as f:
        sector, cluster, mft_lcn, rec = _boot(f)
        b = bytearray(max(rec, sector))
        f.seek(mft_lcn * cluster)
        f.readinto(b)
        if b[0:4] != b"FILE" or not _fixup(b, 0, rec, sector):
            raise NotAvailable("the first MFT record is not readable")
        a = U16(b, 0x14)[0]
        while a < rec:
            t, ln = ATTR_HDR(b, a)
            if t == 0xFFFFFFFF or ln == 0:
                break
            if t == 0x20:
                raise NotAvailable("the MFT is described by an attribute list (very fragmented MFT)")
            if t == 0x80 and b[a + 9] == 0 and b[a + 8]:
                n = U64(b, a + 0x30)[0] // rec
                return {"volume": volume, "sector": sector, "cluster": cluster, "rec": rec, "n": n, "runs": _runs(b, a)}
            a += ln
    raise NotAvailable("the MFT has no data attribute")


def _segments(lay, r0, r1):
    """Disk pieces [(byte offset, bytes, first record)] that hold records r0..r1-1 (sparse parts skipped)."""
    rec, cl = lay["rec"], lay["cluster"]
    # Counted in bytes of the MFT, not in whole records per run: with clusters smaller than a record (512-byte clusters,
    # 1 KB records) a run can end in the middle of a record, and rounding each run down shifted the number of every
    # later record (wrong parents, wrong names). A record split between two runs is the only one left out.
    out, start = [], 0          # start: where this run begins inside the MFT, in bytes
    for lcn, clen in lay["runs"]:
        end = start + clen * cl
        a = max(r0, -(-start // rec))       # first record that begins inside this run
        b = min(r1, end // rec)             # one past the last record that ends inside it
        if a < b and lcn is not None:
            out.append((lcn * cl + a * rec - start, (b - a) * rec, a))
        start = end
        if start >= r1 * rec:
            break
    return out


_counter = None


def _init_worker(counter):
    global _counter
    _counter = counter


def parse_range(lay, r0, r1, chunk_mb=8):
    """Parse records r0..r1-1. Returns compact bytes (picklable, cheap to send between processes):
    (r0, r1, flags, parent, size, alloc, mtime, ext, exts, names, foreign), where ext holds indexes into this
    range's own 'exts' list and 'foreign' lists updates for base records outside the range."""
    m = r1 - r0
    rec, sector = lay["rec"], lay["sector"]
    flags = bytearray(m)
    parent = array("q", bytes(8 * m))
    size = array("q", bytes(8 * m))
    alloc = array("q", bytes(8 * m))
    mtime = array("I", bytes(4 * m))
    ext = array("H", bytes(2 * m))
    names, ext_ix, exts = {}, {"": 0}, [""]
    foreign = {}                                    # base record -> [flags, parent, size, alloc, mtime, name]
    chunk = max(rec, chunk_mb * 1024 * 1024 // rec * rec)
    buf = bytearray(chunk)
    mv = memoryview(buf)
    sig = b"FILE"
    with _open(lay["volume"]) as f:
        for pos, nbytes, first in _segments(lay, r0, r1):
            recno = first
            while nbytes > 0:
                want = min(chunk, nbytes)
                f.seek(pos)
                got = f.readinto(mv[:want])
                if not got:
                    break
                pos += got
                nbytes -= got
                for off in range(0, got - rec + 1, rec):
                    r = recno
                    recno += 1
                    if buf[off:off + 4] != sig:
                        continue
                    fl = U16(buf, off + 0x16)[0]
                    if not fl & 1 or not _fixup(buf, off, rec, sector):
                        continue
                    base = U64(buf, off + 0x20)[0] & REF_MASK
                    tgt = base if base else r
                    local = r0 <= tgt < r1
                    li = tgt - r0
                    fw = None
                    if not local:
                        fw = foreign.get(tgt)
                        if fw is None:
                            fw = foreign[tgt] = [0, 0, 0, 0, 0, None]
                    if not base and local:
                        flags[li] |= F_USED | (F_DIR if fl & 2 else 0)
                    a = off + U16(buf, off + 0x14)[0]
                    end = off + rec
                    name = None
                    pref = 0
                    pnt = 0
                    has_list = False
                    sz = -1
                    al = 0
                    mt = 0
                    fa = 0
                    notlink = 0
                    while a + 8 <= end:
                        t, ln = ATTR_HDR(buf, a)
                        if t == 0xFFFFFFFF or ln == 0 or a + ln > end:
                            break
                        if t == 0x30:
                            if not buf[a + 8]:
                                c = a + U16(buf, a + 0x14)[0]
                                ns = buf[c + 0x41]
                                # the first name wins (hard links count once); a long name replaces a DOS 8.3 one
                                if name is None or (pref == 2 and ns != 2):
                                    name = buf[c + 0x42:c + 0x42 + 2 * buf[c + 0x40]].decode("utf-16-le", "replace")
                                    pref = ns
                                    pnt = U64(buf, c)[0] & REF_MASK
                        elif t == 0x80:
                            if buf[a + 9] == 0:                    # unnamed stream: the file content
                                if buf[a + 8]:
                                    if U64(buf, a + 0x10)[0] == 0:  # the first piece holds the real sizes
                                        al = U64(buf, a + 0x28)[0]
                                        sz = U64(buf, a + 0x30)[0]
                                else:
                                    sz = U32(buf, a + 0x10)[0]
                        elif t == 0x10:
                            if not buf[a + 8]:
                                c = a + U16(buf, a + 0x14)[0]
                                ft = U64(buf, c + 8)[0]
                                if ft > EPOCH:
                                    mt = min(0xFFFFFFFF, (ft - EPOCH) // 10_000_000)
                                fa = U32(buf, c + 0x20)[0]
                        elif t == 0x20:
                            has_list = True
                        elif t == 0xC0:
                            if not buf[a + 8] and U32(buf, a + 0x10)[0] >= 4:
                                if U32(buf, a + U16(buf, a + 0x14)[0])[0] not in LINK_TAGS:
                                    notlink = F_NOTLINK
                        a += ln
                    xf = (F_REPARSE if fa & FA_REPARSE else 0) | (F_CLOUD if fa & FA_CLOUD else 0) | notlink
                    if local:
                        if xf:
                            flags[li] |= xf
                        if sz >= 0:
                            size[li] = sz
                            alloc[li] = al
                        if mt:
                            mtime[li] = mt
                        # the base record's name wins; an extension record's name (another hard link) only
                        # fills in when the base had none, so a file always lands in the same folder
                        if name is not None and (not base or not flags[li] & F_NAMED):
                            parent[li] = pnt
                            flags[li] |= F_NAMED
                        else:
                            name = None
                    else:
                        fw[0] |= xf
                        if sz >= 0:
                            fw[2], fw[3] = sz, al
                        if mt:
                            fw[4] = mt
                        if name is not None and fw[5] is None:
                            fw[1], fw[5] = pnt, name
                    if name is not None and local:
                        dot = name.rfind(".")
                        e = name[dot + 1:].lower() if dot > 0 else ""
                        ix = ext_ix.get(e)
                        if ix is None:
                            ix = len(exts)
                            if ix < 65535:
                                ext_ix[e] = ix
                                exts.append(e)
                            else:
                                ix = 0
                        ext[li] = ix
                        # names are only kept where needed later: folders, big files, files whose size may
                        # still arrive in an extension record, and system files
                        if fl & 2 or has_list or size[li] >= KEEP_NAME_BYTES or name[0] == "$" or name.lower() in _KEEP:
                            names[tgt] = name
                if _counter is not None:
                    with _counter.get_lock():
                        _counter.value += got // rec
    return (r0, r1, bytes(flags), parent.tobytes(), size.tobytes(), alloc.tobytes(), mtime.tobytes(), ext.tobytes(),
            exts, names, foreign)


def read(volume, progress=None, cancelled=None, workers=None):
    """Read and parse the whole MFT of `volume` ("C:") using several processes.

    Returns a dict of arrays indexed by record number: flags (F_*; is_link() tells junctions/symlinks), parent, size (logical bytes), alloc (bytes
    on disk), mtime (unix seconds), ext (index into 'exts'), 'names' {record: name} (folders, big files, system
    files), 'exts', 'n'. None if cancelled. Raises NotAvailable if it can't be read here.
    progress(fraction, records_done) is called while it works."""
    lay = layout(volume)
    n = lay["n"]
    if workers is None:
        workers = max(2, min(8, (os.cpu_count() or 4) // 2))
    parts = []
    if workers > 1 and n > 200_000:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        counter = ctx.Value("q", 0)
        step = -(-n // (workers * 3))                 # a few ranges per worker balances fast and slow cores
        ranges = [(r, min(n, r + step)) for r in range(0, n, step)]
        try:
            pool = ctx.Pool(workers, initializer=_init_worker, initargs=(counter,))
        except Exception:
            pool = None
        if pool is not None:
            try:
                res = [pool.apply_async(parse_range, (lay, a, b)) for a, b in ranges]
                while not all(r.ready() for r in res):
                    if cancelled and cancelled():
                        pool.terminate()
                        return None
                    if progress:
                        progress(min(.99, counter.value / n), counter.value)
                    time.sleep(.1)
                parts = [r.get() for r in res]
                pool.close()
            except Exception:
                pool.terminate()
                parts = []
    if not parts:                                     # single process (small MFT, or the pool could not start)
        parts = [parse_range(lay, 0, n)]
    if progress:
        progress(1.0, n)
    # merge the ranges into whole-volume arrays
    flags = bytearray(n)
    parent = array("q", bytes(8 * n))
    size = array("q", bytes(8 * n))
    alloc = array("q", bytes(8 * n))
    mtime = array("I", bytes(4 * n))
    ext = array("H", bytes(2 * n))
    names, gix, exts = {}, {"": 0}, [""]
    foreign_all = []
    for r0, r1, pf, pp, ps, pa, pm, pe, pexts, pnames, pfor in parts:
        flags[r0:r1] = pf
        parent[r0:r1] = array("q", pp)
        size[r0:r1] = array("q", ps)
        alloc[r0:r1] = array("q", pa)
        mtime[r0:r1] = array("I", pm)
        remap = []
        for e in pexts:
            ix = gix.get(e)
            if ix is None:
                ix = len(exts)
                if ix < 65535:
                    gix[e] = ix
                    exts.append(e)
                else:
                    ix = 0
            remap.append(ix)
        ext[r0:r1] = array("H", [remap[x] for x in array("H", pe)])
        names.update(pnames)
        foreign_all.append(pfor)
    for pfor in foreign_all:                          # extension records whose base record was in another range
        for tgt, (xf, pnt, sz, al, mt, name) in pfor.items():
            if tgt >= n:
                continue
            if name is not None and not flags[tgt] & F_NAMED:    # the base record had no name of its own
                parent[tgt] = pnt
                names[tgt] = name
                dot = name.rfind(".")
                e = name[dot + 1:].lower() if dot > 0 else ""
                ix = gix.get(e)
                if ix is None and len(exts) < 65535:
                    ix = gix[e] = len(exts)
                    exts.append(e)
                ext[tgt] = ix or 0
                xf |= F_NAMED
            flags[tgt] |= xf
            if sz or al:
                size[tgt], alloc[tgt] = sz, al
            if mt:
                mtime[tgt] = mt
    return {"n": n, "flags": flags, "parent": parent, "size": size, "alloc": alloc, "mtime": mtime, "ext": ext,
            "names": names, "exts": exts, "cluster": lay["cluster"], "record": lay["rec"]}


def is_link(f):
    """True for junctions, mount points and symbolic links (flags value of one record)."""
    return f & F_REPARSE and not f & F_NOTLINK

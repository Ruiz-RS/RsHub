"""Minimal reader for .lnk (MS-SHLLINK format) and .url shortcuts, without COM."""
import configparser
import os
import struct

F_IDLIST, F_LINKINFO, F_NAME, F_RELPATH, F_WORKDIR, F_ARGS, F_ICON, F_UNICODE = (
    0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x80)
F_DARWIN = 0x1000


def _cstr(data, off, wide=False):
    if wide:
        end = off
        while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
            end += 2
        return data[off:end].decode("utf-16-le", "replace")
    end = data.find(b"\0", off)
    return data[off:end if end != -1 else len(data)].decode("mbcs", "replace")


def read_lnk(path):
    """Return dict {target, args, workdir, icon, msi} (empty values when missing)."""
    out = {"target": "", "args": "", "workdir": "", "icon": "", "msi": False}
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 20)
    except OSError:
        return out
    if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
        return out
    flags = struct.unpack_from("<I", data, 0x14)[0]
    out["msi"] = bool(flags & F_DARWIN)
    off = 0x4C
    try:
        if flags & F_IDLIST:
            off += 2 + struct.unpack_from("<H", data, off)[0]
        if flags & F_LINKINFO:
            li_size, li_hdr, li_flags, _vol, base_off, _net, suffix_off = struct.unpack_from("<7I", data, off)
            base = suffix = ""
            if li_flags & 1:
                if li_hdr >= 0x24:
                    base_u = struct.unpack_from("<I", data, off + 0x1C)[0]
                    base = _cstr(data, off + base_u, True) if base_u else _cstr(data, off + base_off)
                    suf_u = struct.unpack_from("<I", data, off + 0x20)[0]
                    suffix = _cstr(data, off + suf_u, True) if suf_u else _cstr(data, off + suffix_off)
                else:
                    base = _cstr(data, off + base_off)
                    suffix = _cstr(data, off + suffix_off)
            out["target"] = os.path.join(base, suffix) if suffix else base
            off += li_size
        wide = bool(flags & F_UNICODE)
        for flag, key in ((F_NAME, None), (F_RELPATH, "rel"), (F_WORKDIR, "workdir"), (F_ARGS, "args"),
                          (F_ICON, "icon")):
            if flags & flag:
                n = struct.unpack_from("<H", data, off)[0]
                off += 2
                size = n * 2 if wide else n
                raw = data[off:off + size]
                off += size
                if key:
                    out[key] = raw.decode("utf-16-le" if wide else "mbcs", "replace").split("\0", 1)[0]
        # extra blocks: EnvironmentVariableDataBlock (target with %variables%)
        while off + 8 <= len(data):
            bsize, sig = struct.unpack_from("<II", data, off)
            if bsize < 8:
                break
            if sig == 0xA0000001 and not out["target"]:
                uni = data[off + 8 + 260: off + 8 + 260 + 520]
                t = uni.decode("utf-16-le", "replace").split("\0", 1)[0] or _cstr(data, off + 8)
                out["target"] = t
            off += bsize
        if not out["target"] and out.get("rel"):
            out["target"] = os.path.normpath(os.path.join(os.path.dirname(path), out["rel"]))
    except (struct.error, IndexError):
        pass
    if not out["target"] and out["icon"]:
        out["target"] = out["icon"].split(",")[0]  # last resort: the icon is usually the .exe
        out["from_icon"] = True
    out.pop("rel", None)
    for k in ("target", "workdir", "icon"):
        out[k] = os.path.expandvars(out[k]) if out[k] else ""
    return out


def read_url(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(path, encoding="utf-8")
    except Exception:
        try:
            cp.read(path, encoding="mbcs")
        except Exception:
            return {"target": "", "icon": ""}
    s = cp["InternetShortcut"] if cp.has_section("InternetShortcut") else {}
    return {"target": s.get("URL", ""), "icon": s.get("IconFile", "")}


def read_shortcut(path):
    p = path.lower()
    if p.endswith(".lnk"):
        return read_lnk(path)
    if p.endswith(".url"):
        return read_url(path)
    return {"target": ""}

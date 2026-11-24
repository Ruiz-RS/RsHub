"""Release signing for RsHub (run on the owner's PC only).

  python tools/release_sign.py keygen [--export [FILE]]   create the release key (once)
  python tools/release_sign.py pubkey                      print the public key (hex) for selfupdate.py TRUSTED_KEYS
  python tools/release_sign.py sign FILE... --version X    write FILE.sig next to each file
  python tools/release_sign.py verify FILE SIG --version X check a download (anyone can run this)
  python tools/release_sign.py restore BACKUP_FILE         put the key back on a new PC from the --export backup

The secret key lives in %USERPROFILE%\\.rshub-release\\release.key, encrypted with Windows DPAPI (only this
Windows user on this PC can read it). It must never be inside the repository. `verify` needs no key: it checks
against the keys the app trusts (rshub/engine/selfupdate.py TRUSTED_KEYS) or one given with --pubkey.
What is signed is selfupdate.manifest(): file name, version and SHA-256 of the file.
"""
import argparse
import ctypes
import hashlib
import os
import sys
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rshub import ed25519  # noqa: E402
from rshub.engine import selfupdate  # noqa: E402


# ---------------------------------------------------------------- DPAPI (Windows)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(fn, data, what):
    if sys.platform != "win32":
        raise SystemExit("This tool protects the key with Windows DPAPI: run it on the owner's Windows PC.")
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    api = ctypes.windll.crypt32
    if fn == "protect":
        ok = api.CryptProtectData(ctypes.byref(src), "RsHub release key", None, None, None, 0, ctypes.byref(out))
    else:
        ok = api.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise SystemExit(f"Windows could not {what} the key (is this the same Windows user and PC that created it?).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data):
    return _dpapi("protect", data, "protect")


def unprotect(data):
    return _dpapi("unprotect", data, "unlock")


# ---------------------------------------------------------------- the key
def _inside(path, root):
    path, root = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return path == root or path.startswith(root + os.sep)


def key_dir():
    d = os.environ.get("RSHUB_RELEASE_KEY_DIR") or os.path.join(os.path.expanduser("~"), ".rshub-release")
    d = os.path.abspath(d)
    if _inside(d, ROOT):
        raise SystemExit(f"The key folder must never be inside the repository ({d}).")
    return d


def key_file():
    return os.path.join(key_dir(), "release.key")


def load_seed():
    try:
        with open(key_file(), "rb") as fh:
            seed = unprotect(fh.read())
    except OSError:
        raise SystemExit(f"No release key at {key_file()}. Create it with: python tools/release_sign.py keygen")
    if len(seed) != 32:
        raise SystemExit("The key file is damaged.")
    return seed


def save_seed(seed):
    os.makedirs(key_dir(), exist_ok=True)
    with open(key_file(), "xb") as fh:  # never overwrites an existing key
        fh.write(protect(seed))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- commands
def cmd_keygen(a):
    if os.path.exists(key_file()):
        raise SystemExit(f"A release key already exists at {key_file()}. Not overwriting it.")
    backup = None
    if a.export is not None:
        backup = os.path.abspath(a.export or os.path.join(key_dir(), "release-key-BACKUP.txt"))
        if _inside(backup, ROOT):
            raise SystemExit("The backup must never be inside the repository.")
    seed = os.urandom(32)
    save_seed(seed)
    pub = ed25519.public_key(seed).hex()
    print(f"Release key created: {key_file()}")
    print("It is encrypted with Windows DPAPI: only this Windows user, on this PC, can use it.")
    if backup:
        with open(backup, "x", encoding="utf-8") as fh:
            fh.write("RsHub release key. UNPROTECTED backup: anyone with this can publish updates as you.\n"
                     f"seed: {seed.hex()}\npublic: {pub}\n")
        print(f"\nUNPROTECTED backup written: {backup}")
        print("Move it OFFLINE now (a USB drive in a drawer, or a password manager) and delete this copy.")
        print("If you lose the key (PC reinstalled, disk died) and have no backup, you can never sign another update")
        print("that existing copies accept: everyone would have to download RsHub by hand once more.")
        print("Never put it in the repository, in a cloud-synced folder, or send it to anyone.")
    else:
        print("\nNo backup was written. If this PC is lost or reinstalled, the key is lost with it and updates could")
        print("no longer be signed. To keep a backup, run keygen again with --export on a PC with no key yet.")
    print(f"\nPublic key (paste into TRUSTED_KEYS in rshub/engine/selfupdate.py):\n{pub}")


def cmd_pubkey(a):
    print(ed25519.public_key(load_seed()).hex())


def cmd_restore(a):
    if os.path.exists(key_file()):
        raise SystemExit(f"A release key already exists at {key_file()}. Not overwriting it.")
    seed = None
    with open(a.backup, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("seed:"):
                seed = bytes.fromhex(line.split(":", 1)[1].strip())
    if not seed or len(seed) != 32:
        raise SystemExit("No seed found in that file.")
    save_seed(seed)
    print("Key restored. Public key:", ed25519.public_key(seed).hex())


def cmd_sign(a):
    seed = load_seed()
    pub = ed25519.public_key(seed)
    if selfupdate.TRUSTED_KEYS and pub.hex() not in selfupdate.TRUSTED_KEYS:
        print("WARNING: this key is not in TRUSTED_KEYS of selfupdate.py: the app would refuse these signatures.",
              file=sys.stderr)
    for f in a.files:
        name = os.path.basename(f)
        msg = selfupdate.manifest(name, a.version, sha256_file(f))
        sig = ed25519.sign(seed, msg)
        if not ed25519.verify(pub, msg, sig):
            raise SystemExit("Internal check failed: the signature does not verify.")
        with open(f + ".sig", "wb") as fh:
            fh.write(sig)
        print(f"signed {name} -> {name}.sig")


def cmd_verify(a):
    keys = [a.pubkey] if a.pubkey else selfupdate.TRUSTED_KEYS
    if not keys:
        raise SystemExit("No public key to check against: this copy of the tool trusts none yet. Use --pubkey HEX.")
    with open(a.sig, "rb") as fh:
        sig = fh.read()
    name = os.path.basename(a.file)
    if selfupdate.signature_ok(name, a.version, sha256_file(a.file), sig, keys):
        print(f"OK: {name} is version {a.version}, signed by RsHub's author.")
        return 0
    print(f"NOT VALID: {name} does not match its signature for version {a.version} (or the signer is unknown). Do not run it.")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="RsHub release signing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="create the release key (once)")
    k.add_argument("--export", nargs="?", const="", default=None, metavar="FILE",
                   help="also write an UNPROTECTED backup (keep it offline)")
    sub.add_parser("pubkey", help="print the public key")
    r = sub.add_parser("restore", help="restore the key from an --export backup")
    r.add_argument("backup")
    s = sub.add_parser("sign", help="sign release files")
    s.add_argument("files", nargs="+")
    s.add_argument("--version", required=True)
    v = sub.add_parser("verify", help="verify a downloaded file")
    v.add_argument("file")
    v.add_argument("sig")
    v.add_argument("--version", required=True)
    v.add_argument("--pubkey", help="hex public key (default: the keys the app trusts)")
    a = ap.parse_args(argv)
    return {"keygen": cmd_keygen, "pubkey": cmd_pubkey, "restore": cmd_restore, "sign": cmd_sign, "verify": cmd_verify}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())

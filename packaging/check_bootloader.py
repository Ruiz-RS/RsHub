"""Tells whether the PyInstaller installed in this Python carries a bootloader compiled here or the prebuilt one.

    python packaging/check_bootloader.py            exit 0: compiled from source (hashes differ from the official wheel)
                                                    exit 1: it is the prebuilt one (a silent fallback, or never compiled)
    python packaging/check_bootloader.py --report   only prints, always exit 0

The bootloader is the small program (run.exe, and runw.exe for windowed apps) every PyInstaller program is built on.
The one PyInstaller ships is the same bytes in countless programs, some of them malware, and antivirus
machine-learning models flag programs that match it. Release builds compile their own (see build.ps1 and
.github/workflows/build.yml); this script is how they prove it happened: it downloads the official Windows wheel of
the same version (pip download, nothing is installed) and compares the SHA-256 of run.exe and runw.exe."""
import hashlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
import zipfile

FILES = ("run.exe", "runw.exe")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def installed():
    import PyInstaller
    folder = os.path.join(PyInstaller.HOMEPATH, "PyInstaller", "bootloader", PyInstaller.PLATFORM)
    return {f: sha256(os.path.join(folder, f)) for f in FILES if os.path.isfile(os.path.join(folder, f))}


def official(version):
    """Hashes of the bootloaders inside the official win_amd64 wheel of this version."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--no-deps", "--only-binary", ":all:",
                        "--platform", "win_amd64", "--python-version", "3.13", "pyinstaller==" + version, "-d", d, "-q"], check=True)
        wheel = next(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".whl"))
        out = {}
        with zipfile.ZipFile(wheel) as z:
            for n in z.namelist():
                if n.replace("\\", "/").startswith("PyInstaller/bootloader/Windows-64bit-intel/") and os.path.basename(n) in FILES:
                    out[os.path.basename(n)] = hashlib.sha256(z.read(n)).hexdigest()
        return out


def main():
    version = importlib.metadata.version("pyinstaller")
    mine, theirs = installed(), official(version)
    print(f"PyInstaller {version}")
    same = []
    for f in FILES:
        print(f"  {f:9} installed {mine.get(f, '-')[:16]}  official {theirs.get(f, '-')[:16]}")
        if not mine.get(f) or not theirs.get(f):
            print(f"  {f}: missing, cannot compare")
            return 1
        if mine[f] == theirs[f]:
            same.append(f)
    if same:
        print("The bootloader is the prebuilt one (identical to the official wheel):", ", ".join(same))
        return 0 if "--report" in sys.argv else 1
    print("The bootloader was compiled from source (differs from the official wheel).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# PyInstaller recipe for RsHub.exe. Run it through packaging/build.ps1, which prepares the assets first.
#
# Choices that keep the installed app as fast as running from source:
#   * one folder (onedir), not one file: a onefile exe unpacks the whole runtime to %TEMP% on every launch,
#     onedir starts straight from Program Files.
#   * no UPX: compressed DLLs must be unpacked in memory on every start, and packed executables are a classic
#     antivirus false-positive trigger (which also makes Defender scan them longer).
#   * a clean build environment (.buildenv) with pinned versions, so only what RsHub imports ships.
#   * the bootloader (the stub every PyInstaller program starts with) is compiled from source in release builds
#     (packaging/build.ps1, packaging/check_bootloader.py): the prebuilt one is byte-identical in thousands of other
#     programs, some of them malware, and antivirus machine-learning models match on it.
#   * everything else in _internal, so the install folder holds just RsHub.exe and that one folder.
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)  # noqa: F821  (SPECPATH is provided by PyInstaller)
ASSETS = os.path.join(ROOT, "build", "assets")

# the page reads each language in one request: make the merged dictionaries before the UI folder is collected
sys.path.insert(0, ROOT)
from rshub import i18n as _i18n  # noqa: E402
_i18n.build_bundles(os.path.join(ROOT, "rshub", "ui", "i18n"))

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "rshub", "ui"), os.path.join("rshub", "ui")),
        (os.path.join(ASSETS, "project_stats.json"), "rshub"),
    ],
    # engine modules are loaded by name when the UI first asks for them
    hiddenimports=collect_submodules("rshub.engine") + ["pystray._win32"],
    excludes=["tkinter", "_tkinter", "test", "idlelib", "lib2to3", "pydoc_data", "ensurepip", "venv",
              "turtledemo", "PIL.ImageTk", "PIL.ImageQt", "webview.platforms.qt", "webview.platforms.gtk",
              "webview.platforms.cocoa", "webview.platforms.android", "webview.platforms.cef",
              # nothing at runtime imports these (setuptools/distutils only get in through cffi's build helpers);
              # less to ship, less for Defender to scan on the first run
              "setuptools", "pkg_resources", "pydoc", "doctest", "unittest",
              "xmlrpc", "ftplib", "curses", "_pyrepl", "asyncio", "pdb",
              # Pillow's AVIF codec (7.9 MB): the app icons come as PNG/ICO/WebP, never AVIF (WebP is kept)
              "PIL._avif", "PIL.AvifImagePlugin"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RsHub",
    icon=os.path.join(ASSETS, "RsHub.ico"),
    version=os.path.join(ASSETS, "version_info.txt"),  # CompanyName, FileDescription, ProductName... (make_assets.py)
    manifest=os.path.join(SPECPATH, "RsHub.manifest"),  # asInvoker, supportedOS, dpiAware (see the file)
    console=False,
    disable_windowed_traceback=True,  # a failing worker process must not pop up a raw Python error dialog
    upx=False,
    uac_admin=False,  # starts as a normal user; administrator mode is the user's choice inside the app
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="RsHub",
)

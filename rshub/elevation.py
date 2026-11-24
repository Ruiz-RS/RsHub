"""Administrator mode: the scheduled task that opens RsHub elevated without the Windows prompt, deciding at the
start whether this copy must relaunch elevated, and the relaunch itself.

Only an all-users install gets the task (uses_admin_task): its program lives where only administrators can change it
(Program Files, or a folder the installer registered for all users). A per-user install (%LOCALAPPDATA%\\Programs),
the portable copy and the source version live in folders any program the user runs can write to: a task there would
run whatever RsHub.exe is in that folder now, elevated and without a prompt. Those copies ask Windows (UAC) at
each start instead, and an elevated RsHub removes a task an earlier version left pointing at such a folder
(remove_unsafe_admin_task)."""
import ctypes
import os
import sys
import time
from pathlib import Path

from . import config, instance, paths, settings
from .log import phase

ADMIN_TASK = r"RsHub\Open as administrator"
START_HIDDEN = str(Path(paths.local_dir()) / "start-hidden")  # see elevate_on_start
CHECKS_STAMP = str(Path(paths.local_dir()) / "admin-checks")  # see admin_maintenance
LEGACY_ADMIN_TASKS = (r"RsHub\Abrir como administrador",)  # name used by earlier versions


PROGRAM_FILES = ("905E63B6-C1BF-494E-B29C-65B732D3D21A", "6D809377-6AF0-444B-8957-A3773F02200E",  # ProgramFiles, X64
                 "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E")  # ProgramFilesX86


def _known_folder(guid):
    """A Windows known folder, asked from Windows (never the user-settable %ProgramFiles% variables), or None."""
    from .engine.removal.paths import _known_folder as known
    return known(guid)


def _under(path, root):
    if not path or not root:
        return False
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _registered_for_all_users(folder):
    """True if the installer registered this folder as an all-users install: its HKLM uninstall entry, which only an
    administrator can write. The per-user entry (HKCU) never counts here: any program the user runs can write it."""
    import winreg
    from .engine.selfupdate import APP_ID
    here = os.path.normcase(os.path.abspath(folder)).rstrip("\\")
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + APP_ID,
                                0, winreg.KEY_READ | view) as k:
                loc = winreg.QueryValueEx(k, "InstallLocation")[0]
        except OSError:
            continue
        if loc and os.path.normcase(os.path.abspath(loc)).rstrip("\\") == here:
            return True
    return False


def per_machine_folder(folder):
    """The installer's and selfupdate.per_machine()'s rule: installed for all users if the installer registered the
    folder that way, else if it lives in Program Files. Read only from what a program running as the user can't
    change (the HKLM entry, Windows' own Program Files folders)."""
    if not folder:
        return False
    try:
        if _registered_for_all_users(folder):
            return True
    except Exception:
        pass
    return any(_under(folder, _known_folder(g)) for g in PROGRAM_FILES)


def uses_admin_task():
    """Whether "Always open as administrator" may use the scheduled task: only the installed program of an all-users
    install. Per-user installs, the portable copy and the source version ask Windows at each start (module docstring)."""
    if config.PORTABLE or not config.FROZEN:
        return False
    try:
        return per_machine_folder(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:
        return False


def ensure_admin_task():
    """Creates (or updates) the scheduled task that opens RsHub elevated without the UAC prompt (all-users
    installs only: uses_admin_task)."""
    if not uses_admin_task():
        return False  # per-user, portable or source: Windows asks each time instead of a task running this folder's exe
    import subprocess
    from . import schtask
    exe, args = config.launch_cmd("--from-task")
    ok = schtask.create(ADMIN_TASK, exe, args, "Opens RsHub as administrator without the Windows prompt "
                                               "(Settings > Administrator > Always open as administrator).")
    if ok:  # replace tasks left by earlier versions under another name
        for old in LEGACY_ADMIN_TASKS:
            subprocess.run(["schtasks", "/delete", "/tn", old, "/f"], capture_output=True, creationflags=0x08000000)
    return ok


def repair_update_task():
    """The updater's task, if an older installer made it the old way (it didn't run on battery): made again.
    False when the check itself failed."""
    if config.PORTABLE or not config.FROZEN:
        return True
    try:
        from . import schtask
        from .engine import selfupdate
        if selfupdate._task_exists() and schtask.outdated(schtask.query_xml(selfupdate.TASK)):
            selfupdate.ensure_task()
        return True
    except Exception as e:
        print("update task:", e)
        return False


def delete_admin_task():
    import subprocess
    for name in (ADMIN_TASK, *LEGACY_ADMIN_TASKS):
        subprocess.run(["schtasks", "/delete", "/tn", name, "/f"], capture_output=True, creationflags=0x08000000)


def task_safe_target(command):
    """A task may go on starting a program elevated without a prompt only if that program is the RsHub.exe of an
    all-users install (a folder only administrators can change). Anything else (a per-user install, a portable copy,
    python with a run.py in the user's documents) is a program a user-level process could replace."""
    if not command:
        return False
    return os.path.basename(command).lower() == "rshub.exe" and per_machine_folder(os.path.dirname(command))


def remove_unsafe_admin_task():
    """Run by an elevated RsHub (only then can the task be deleted): the "open as administrator" tasks an earlier
    version made for a per-user install, a portable or a source copy are removed, whichever copy they start. A task
    that starts an all-users install stays. Returns the names removed."""
    import subprocess
    gone = []
    for name in (ADMIN_TASK, *LEGACY_ADMIN_TASKS):
        found = task_command(name)
        if found is None or task_safe_target(found[0]):
            continue
        r = subprocess.run(["schtasks", "/delete", "/tn", name, "/f"], capture_output=True, creationflags=0x08000000)
        if r.returncode == 0:
            gone.append(name)
            print(f"[admin] removed the task {name!r}: it opened {found[0]!r} as administrator without a prompt, "
                  "from a folder the user's programs can change", flush=True)
    return gone


def admin_task_current():
    """True when the administrator task exists, opens this copy and was made the current way (schtask.outdated).
    One schtasks query."""
    from . import schtask
    xml = schtask.query_xml(ADMIN_TASK)
    found = _command_in(xml)
    return found is not None and _opens_this_copy(found) and not schtask.outdated(xml)


def sync_admin_task():
    """An elevated copy: an all-users install with the setting on makes its task again when it is missing, opens
    another copy or was made the old way (it is no longer replaced at every start); any other copy removes an unsafe
    one an earlier version left (remove_unsafe_admin_task). False when that failed."""
    try:
        if uses_admin_task():
            if settings.read().get("alwaysAdmin") and not admin_task_current():
                ensure_admin_task()
        else:
            remove_unsafe_admin_task()
        return True
    except Exception as e:
        print("admin task:", e, flush=True)
        return False


def _checks_key():
    from . import VERSION
    return f"{VERSION}|{os.path.normcase(sys.executable)}"


def admin_maintenance():
    """What an elevated copy checks at start, with as few schtasks.exe runs as it can: a burst of them at every start
    of a new, unsigned program is what antivirus behaviour engines weigh, and these checks rarely find anything.
      * an all-users install with "Always open as administrator" on: its task (sync_admin_task), except when this copy
        was opened by that very task (--from-task: it exists and opens this copy);
      * the leftovers of earlier versions: an unsafe administrator task (per-user, portable and source copies) and an
        update task made the old way (repair_update_task). Looked for once per version and program path (a stamp in
        the local data folder), and again until the check succeeds."""
    try:
        with open(CHECKS_STAMP, encoding="utf-8") as fh:
            due = fh.read().strip() != _checks_key()
    except OSError:
        due = True
    ok = True
    if due or (uses_admin_task() and "--from-task" not in sys.argv):
        ok = sync_admin_task()
    if not due:
        return
    ok = repair_update_task() and ok
    if ok:
        try:
            Path(CHECKS_STAMP).parent.mkdir(parents=True, exist_ok=True)
            with open(CHECKS_STAMP, "w", encoding="utf-8") as fh:
                fh.write(_checks_key())
        except OSError:
            pass


def wants_elevation():
    """Permanent administrator mode is on and this copy is not elevated (nor a test copy, nor already the elevated one)."""
    if config.is_admin() or config.ISOLATED or "--no-elevate" in sys.argv or "--from-task" in sys.argv:
        return False
    try:
        return bool(settings.read().get("alwaysAdmin"))
    except AttributeError:
        return False


def elevate_on_start():
    """If the user chose permanent administrator mode and this copy is not elevated: relaunch elevated
    (first through the scheduled task, with no prompt; if it does not exist, with the UAC prompt). Returns True if we must exit.
    The start screen is already up ("Opening as administrator…") and stays until the elevated copy takes over."""
    if not wants_elevation():
        return False
    hidden = "--tray" in sys.argv
    phase("elevating")
    if hidden:  # the task can't take arguments: the elevated copy reads this note and stays in the tray
        try:
            Path(START_HIDDEN).parent.mkdir(parents=True, exist_ok=True)
            Path(START_HIDDEN).touch()
        except OSError:
            pass
    splash = _hand_over_splash()
    ran_at = time.time()
    # only an all-users install runs its task: a per-user copy never starts one (the elevated copy Windows' prompt
    # opens below removes a task an earlier version made for it)
    if uses_admin_task() and task_opens_this_copy(ADMIN_TASK) and _run_task(ADMIN_TASK):
        # a copy the task did start but that is slow to reach the lock (a cold logon, an antivirus scan) gets up to a
        # minute: ending it after 8 s brought the Windows prompt at every such logon
        if elevated_copy_started() or (_copy_started_since(ran_at) and elevated_copy_started(52)):
            phase("elevated copy started (task)")
            _until_taken_over(splash)
            return True
        # Windows kept the task waiting (a task made by an older version doesn't start on battery) or it failed:
        # drop it and ask Windows the usual way; the elevated copy then makes the task again, the right way
        from . import schtask
        schtask.end(ADMIN_TASK)
    # no task, or it opens another copy (e.g. the source version before installing): ask Windows once;
    # the elevated copy then points the task at itself
    exe, args = config.launch_cmd("--no-elevate" + (" --tray" if hidden else ""))
    if ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, config.base_dir(), 1) > 32:
        elevated_copy_started(10)  # keep the start screen up until the elevated copy has its own
        phase("elevated copy started (UAC)")
        _until_taken_over(splash)
        return True
    print("[start] administrator mode was declined: opening in normal mode", flush=True)
    if splash is not None:
        splash.hand_over(False)
    return False  # the user said no: stay in normal mode


def _run_task(name):
    """schtasks /run: True if Windows accepted it. A Task Scheduler that does not answer in 15 s (or schtasks that
    can't start) is a failed run, never an exception: that ended the start with no window at all."""
    import subprocess
    try:
        return subprocess.run(["schtasks", "/run", "/tn", name], capture_output=True, creationflags=0x08000000,
                              timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        print("[start] the administrator task could not be run:", e, flush=True)
        return False


def _hand_over_splash():
    """The start screen up in this process, marked for the elevated copy to take over (splash.hand_over), or None."""
    try:
        from . import splash
        if splash.showing():
            splash.hand_over()
            return splash
    except Exception as e:
        print("splash handoff:", e)
    return None


def _until_taken_over(splash, seconds=4):
    """This copy is about to exit: give the elevated copy's start screen (bounded) time to replace this one, so the
    screen is never empty in between."""
    if splash is None:
        return
    end = time.monotonic() + seconds
    while time.monotonic() < end and not splash.handed() and splash.showing():
        time.sleep(0.02)


def _copy_started_since(t):
    """True if another copy of this program has started since `t` (the one the task launched, still starting)."""
    try:
        from .instance import other_copies
        return any(p.create_time() >= t - 1 for p in other_copies())
    except Exception:
        return False


def elevated_copy_started(seconds=8):
    """After running the administrator task: True once the elevated copy holds the single-instance mutex (it takes
    it within a second or two of starting). From a normal copy an elevated mutex can't be opened: access denied
    also means it exists."""
    k32 = ctypes.windll.kernel32
    k32.OpenMutexW.restype = ctypes.c_void_p
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        h = k32.OpenMutexW(0x00100000, False, instance.mutex_name())  # SYNCHRONIZE
        err = k32.GetLastError()
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
            return True
        if err == 5:  # ERROR_ACCESS_DENIED: it exists, owned by the elevated copy
            return True
        time.sleep(0.15)
    return False


def task_command(name):
    """(command, arguments) of the scheduled task's action, with the XML's escapes undone ("&amp;" in a path with "&"
    never matched: the Windows prompt at every start), or None if there is no such task."""
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", name, "/xml"], capture_output=True, timeout=10,
                           creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    raw = r.stdout
    return _command_in(raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("oem", "replace"))


def _command_in(xml):
    """(command, arguments) of a task definition's action, with the XML's escapes undone, or None."""
    import html
    import re
    if not xml:
        return None
    cmd = re.search(r"<Command>(.*?)</Command>", xml, re.S)
    arg = re.search(r"<Arguments>(.*?)</Arguments>", xml, re.S)
    if not cmd:
        return None
    return html.unescape(cmd.group(1)).strip().strip('"'), (html.unescape(arg.group(1)).strip() if arg else "")


def task_opens_this_copy(name):
    """True if the scheduled task exists and launches this same program (the exe, or run.py from source)."""
    found = task_command(name)
    return found is not None and _opens_this_copy(found)


def _opens_this_copy(found):
    """True if a task's (command, arguments) launch this same program (the exe, or run.py from source)."""
    exe, _args = config.launch_cmd()

    def norm(t):
        return os.path.normcase(t.strip().strip('"'))
    if norm(found[0]) != norm(exe):
        return False
    return config.FROZEN or norm(str(Path(config.base_dir()) / "run.py")) in norm(found[1])


def relaunch_elevated(extra=()):
    """Asks Windows (UAC) for an elevated copy of this program that waits for this process to exit (it frees the
    single-instance lock and the WebView2 data). True if Windows gave permission; the caller then quits.
    extra: more arguments for the new copy (--resume <ticket hash>: rshub/resume.py)."""
    args = [a for a in sys.argv[1:] if a not in ("--tray",)]
    for opt in ("--wait-pid", "--resume"):
        while opt in args:
            i = args.index(opt)
            del args[i:i + 2]
    args += ["--wait-pid", str(os.getpid()), *extra]
    params = " ".join(f'"{a}"' for a in args)
    if not config.FROZEN:
        params = f'"{Path(config.base_dir()) / "run.py"}" {params}'
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", config.gui_python(), params, config.base_dir(), 1) > 32

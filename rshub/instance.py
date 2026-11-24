"""One RsHub per Windows session: the named mutex, the local port a second copy uses to ask the first to show
itself (or to quit, for the installer), the wait for the previous copy during a handoff, and taking over from a copy
that is stuck. Only light imports: this runs before the window exists."""
import ctypes
import os
import sys
import threading
import time
from pathlib import Path

from . import config
from .i18n import tr
from .log import fatal, phase

def session_id(pid=None):
    """The Windows session a process runs in (this one by default); 0 when Windows does not say."""
    sid = ctypes.c_ulong(0)
    try:
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid() if pid is None else int(pid), ctypes.byref(sid)):
            return sid.value
    except Exception:
        pass
    return 0


# single instance: a second copy asks the first one to show itself, then exits. One port per Windows session, so
# a second user signed in on the same PC (fast user switching, Remote Desktop) gets their own RsHub
SINGLE_PORT = 47830 + session_id() % 70
# RSHUB_LOCK_ID: isolated test copies that must see each other (the single-instance tests) share this id
LOCK_ID = os.environ.get("RSHUB_LOCK_ID") or str(os.getpid())
if config.ISOLATED:
    SINGLE_PORT = 47900 + sum(map(ord, LOCK_ID)) % 90


def _port_file():
    """Where the running copy writes the port it listens on when SINGLE_PORT was taken (local data folder)."""
    from . import paths
    tag = f"test{LOCK_ID}" if config.ISOLATED else str(session_id())
    return str(Path(paths.local_dir()) / f"instance-{tag}.port")


def _ports():
    """The ports a running copy may listen on: SINGLE_PORT, then the one it wrote down when that was taken."""
    out = [SINGLE_PORT]
    try:
        with open(_port_file(), encoding="ascii") as fh:
            p = int(fh.read().strip())
        if 1024 <= p <= 65535 and p != SINGLE_PORT:
            out.append(p)
    except (OSError, ValueError):
        pass
    return out


def ask_running_to_show(wait=1.5):
    """Asks the running copy to show its window. True only if it ANSWERED that it did (it replies "ok" once the
    window is up), so a copy that is quitting, hung or deaf is told apart from one that obeyed."""
    import socket
    for port in _ports():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=min(wait, 0.5)) as s:
                s.settimeout(wait)
                s.sendall(b"show")
                if s.recv(4).startswith(b"ok"):
                    return True
        except OSError:
            continue
    return False


def listen_single(api):
    """The running copy's side of the single instance: "show" and "quit" on SINGLE_PORT. If another program holds that
    port, any free one, written down in _port_file() for the next copy: with no listener at all, every later double
    click took this healthy copy for stuck and ended it a minute later. Returns the listening socket, or None."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", SINGLE_PORT))
        moved = None
    except OSError as e:
        try:
            srv.bind(("127.0.0.1", 0))
        except OSError:
            srv.close()
            print("[start] no port for the single-instance listener:", e, flush=True)
            return None
        moved = srv.getsockname()[1]
        print(f"[start] port {SINGLE_PORT} is taken ({e}): listening on {moved}", flush=True)
    try:
        if moved:
            Path(_port_file()).parent.mkdir(parents=True, exist_ok=True)
            with open(_port_file(), "w", encoding="ascii") as fh:
                fh.write(str(moved))
        else:
            os.remove(_port_file())  # an older copy's note: nobody needs to try that port any more
    except OSError:
        pass
    srv.listen(2)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(2)  # a client that connects and says nothing must not block "show" or "quit"
                    msg = conn.recv(16)
                    if msg.startswith(b"show"):
                        if getattr(api, "_quitting", False):
                            continue  # no answer: the new copy waits for the lock this one is about to free
                        t = threading.Thread(target=api.show_main, daemon=True, name="single-show")
                        t.start()
                        t.join(1.2)  # a window thread that is stuck must not keep the other copy waiting
                        if not t.is_alive() and not getattr(api, "_quitting", False):
                            conn.sendall(b"ok")
                    elif msg.startswith(b"quit"):  # the installer closes the running copy before updating it
                        conn.sendall(b"bye")
                        threading.Timer(0.05, lambda: api.quit(True)).start()
            except OSError:
                time.sleep(0.05)  # one bad client never ends the listener
                continue
    threading.Thread(target=loop, daemon=True, name="single").start()
    return srv


def wait_for_previous(seconds=10):
    """--wait-pid N: when relaunching as administrator, wait (bounded) for the previous copy to close. If it is
    still there after that, it is stuck quitting: it is ended, so the handoff can never deadlock."""
    if "--wait-pid" not in sys.argv:
        return
    try:
        pid = int(sys.argv[sys.argv.index("--wait-pid") + 1])
    except (IndexError, ValueError):
        return
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x00100000 | 0x0001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
    if not h:
        return  # already gone (or not ours to wait for)
    try:
        if k32.WaitForSingleObject(ctypes.c_void_p(h), int(seconds * 1000)) == 0:
            phase("previous copy exited")
            return
        print(f"[start] the previous copy (pid {pid}) is still there after {seconds} s: ending it", flush=True)
        k32.TerminateProcess(ctypes.c_void_p(h), 0)
        k32.WaitForSingleObject(ctypes.c_void_p(h), 3000)
        phase("previous copy ended")
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


_MUTEX = None  # held for the whole life of the process
_MUTEX_NAME = r"Local\RsHub.SingleInstance"


def mutex_name():
    return _MUTEX_NAME + (f".test{LOCK_ID}" if config.ISOLATED else "")


def mutex_exists():
    """True if another copy holds the single-instance mutex (an elevated copy's mutex can't be opened from a
    normal one: access denied also means it exists)."""
    k32 = ctypes.windll.kernel32
    k32.OpenMutexW.restype = ctypes.c_void_p
    h = k32.OpenMutexW(0x00100000, False, mutex_name())  # SYNCHRONIZE
    err = k32.GetLastError()
    if h:
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    return err == 5


def claim_single_instance(wait=0.0):
    """One RsHub per Windows session, normal or elevated: a named mutex. True if this copy got it. With
    `wait` (a copy started by the scheduled task or relaunched as administrator) it keeps trying that many
    seconds, to give the previous copy time to exit."""
    global _MUTEX
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    end = time.monotonic() + wait
    while True:
        h = k32.CreateMutexW(None, False, mutex_name())
        err = k32.GetLastError()
        if h and err != 183:  # 183 = ERROR_ALREADY_EXISTS
            _MUTEX = h
            return True
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
        # no handle with error 5 = it exists but belongs to an elevated copy: also "already running"
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)


def other_copies():
    """The other RsHub processes that could be holding the lock (not workers, updaters or installer helpers). Only
    this Windows session's: the lock is per session, and another user's RsHub (fast user switching, Remote
    Desktop, an all-users install) never holds this session's lock, so it must never be taken for stuck and ended."""
    try:
        import psutil
    except ImportError:
        return []
    me, out = os.getpid(), []
    here = session_id()
    mine = os.path.normcase(sys.executable)
    run_py = os.path.normcase(str(Path(config.base_dir()) / "run.py"))
    for pr in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if pr.info["pid"] == me:
                continue
            cmd = [os.path.normcase(a) for a in (pr.info["cmdline"] or [])]
            if any(a.startswith(("--multiprocessing", "--self-update", "--finish-update", "--quit", "--setup",
                                 "--admin-task", "--update-task", "--uninstall-cleanup")) for a in cmd):
                continue
            if config.FROZEN:
                same = (pr.info["name"] or "").lower() == os.path.basename(mine) and \
                    (pr.info["exe"] is None or os.path.normcase(pr.info["exe"]) == mine)
            else:
                same = run_py in cmd
            if same and here and session_id(pr.info["pid"]) not in (0, here):  # (0: Windows did not say: keep it)
                continue
            if same:
                out.append(pr)
        except (psutil.Error, OSError):
            continue
    return out


def _age(pr):
    """Seconds since the process started (0 when unknown, so it is not given extra time)."""
    try:
        return max(0.0, time.time() - pr.create_time())
    except Exception:
        return 60.0


def take_over_running_copy():
    """The single-instance lock is taken. Returns True when this copy got it (and must start), False when it has
    nothing more to do: the running copy showed itself, or the user was told why not.
      1. ask it to show itself (it answers "ok" once its window is up);
      2. no answer: it may be starting, or quitting: keep asking and trying for the lock for a while, with the
         start screen saying "Opening RsHub…";
      3. still nothing: it is stuck: end it (only an old one: a copy that is just starting is given time) and
         take over;
      4. cannot end it (an administrator copy, from a normal one): say so in a message, never exit silently."""
    phase("another copy holds the lock")
    try:
        from . import splash
        if splash.showing():
            splash.status(tr("Opening RsHub…"))
    except Exception:
        pass
    if ask_running_to_show(1.5):
        phase("running copy showed itself")
        return False
    t0 = time.monotonic()
    while time.monotonic() - t0 < 12:
        if claim_single_instance(0):
            phase("lock released by the other copy")
            return True
        if ask_running_to_show(0.6):
            phase("running copy showed itself (late)")
            return False
        time.sleep(0.2)
    others = other_copies()
    # a copy that is still starting (or installing a staged update) has its start screen up: give it its time
    # (the update can take a couple of minutes); one with no start screen and no answer is stuck
    try:
        from . import splash
        end = time.monotonic() + 130
        while time.monotonic() < end and splash.other_showing():
            if claim_single_instance(0):
                return True
            if ask_running_to_show(0.6):
                return False
            time.sleep(0.3)
    except Exception:
        pass
    # a copy started less than a minute ago may just be slow to start (a cold boot, started in the tray with no start
    # screen): it gets that minute before it is taken for stuck
    end = time.monotonic() + max([0.0] + [60 - _age(p) for p in others])
    while time.monotonic() < end:
        if claim_single_instance(0):
            return True
        if ask_running_to_show(0.6):
            return False
        time.sleep(0.3)
    print(f"[start] the running copy does not answer ({[p.pid for p in others]}): ending it", flush=True)
    try:
        from . import splash
        if splash.showing():
            splash.status(tr("RsHub did not answer. Closing it…"))
    except Exception:
        pass
    for pr in others:
        try:
            for ch in pr.children(recursive=True):
                try:
                    ch.kill()
                except Exception:
                    pass
            pr.kill()
        except Exception as e:
            print("[start] could not end", pr.pid, e, flush=True)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if claim_single_instance(0):
            phase("took over after ending the stuck copy")
            return True
        time.sleep(0.2)
    fatal(tr("RsHub is already running but is not answering, and it could not be closed (it may be running "
              "as administrator). End it from Task Manager and open RsHub again."))
    return False


def quit_running():
    """--quit: asks the running copy to close (installer and uninstaller) and waits until it is gone."""
    import socket
    for port in _ports():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5) as s:
                s.sendall(b"quit")
                s.settimeout(3)
                if s.recv(8) != b"bye":
                    continue
        except OSError:
            continue
        for _ in range(80):  # up to 8 s
            time.sleep(0.1)
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            except OSError:
                break
        break
    wait_other_copies_gone()  # not answering (or not running): make sure it is gone anyway


def wait_other_copies_gone(seconds=12):
    """Waits until no other RsHub.exe from this folder is running; if one is still there after the wait, it
    is ended, so the files can be replaced or removed."""
    try:
        import psutil
    except ImportError:
        return
    me, exe = os.getpid(), os.path.normcase(sys.executable)

    def others():
        out = []
        for pr in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if pr.info["pid"] != me and (pr.info["name"] or "").lower() == os.path.basename(exe).lower() \
                        and (pr.info["exe"] is None or os.path.normcase(pr.info["exe"]) == exe):
                    out.append(pr)
            except (psutil.Error, OSError):
                continue
        return out
    if not config.FROZEN:
        return
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not others():
            return
        time.sleep(0.2)
    for pr in others():
        try:
            pr.kill()
        except (psutil.Error, OSError):
            pass

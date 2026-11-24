"""Run PowerShell and console programs without opening windows, in UTF-8, with live output when needed."""
import json
import os
import queue
import subprocess
import threading
import time

from ..i18n import tr

NO_WINDOW = 0x08000000
_PRE = ("$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Continue'; "
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; ")
# At startup several modules query Windows at once; a dozen PowerShell processes in parallel starve each
# other and hit their timeouts on a busy PC. At most 5 run at the same time; the rest wait their turn,
# and a script's timeout only starts counting once it actually runs. Slow reads (a timeout of 60 s or more:
# the Store apps list, driver inventory...) may hold at most 2 of those slots, so they can never leave the quick
# ones queueing behind them (seen as Super Optimizer and Privacy "loading" for minutes in administrator mode,
# where those slow reads take longest). Nobody waits for a slot forever: after SLOT_WAIT seconds it gives up
# with the usual "Windows took too long" error, so the screen shows a message instead of loading for good.
_SLOTS = threading.BoundedSemaphore(5)
_SLOW = threading.BoundedSemaphore(2)
SLOT_WAIT = 150


class _Slot:
    def __init__(self, slow):
        self.gates = (_SLOW, _SLOTS) if slow else (_SLOTS,)
        self.held = []

    def __enter__(self):
        for g in self.gates:
            if not g.acquire(timeout=SLOT_WAIT):
                self.__exit__()
                raise _timeout_error()
            self.held.append(g)
        return self

    def __exit__(self, *a):
        while self.held:
            self.held.pop().release()


class ToolError(RuntimeError):
    """A Windows tool did not answer or is missing. The message is short, translated and fit for the user."""


def _missing(cmd):
    exe = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else str(cmd)
    name = os.path.basename(str(exe))
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return ToolError(tr("{tool} isn't available on this PC.", tool=name or "Windows"))


def _timeout_error():
    return ToolError(tr("Windows took too long to answer. Try again."))


def kill_tree(p):
    """Kills a process and everything it started (an installer launched by winget, a child of PowerShell)."""
    try:
        if p.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def ps(script, timeout=None):
    """Returns the output (text) of a PowerShell script. Raises ToolError (short, translated) if PowerShell is
    missing or takes longer than `timeout` (90 s when not given); on a timeout the whole process tree is killed.
    An explicit timeout of 60 s or more marks a slow read (see _SLOW)."""
    slow = timeout is not None and timeout >= 60
    timeout = timeout or 90
    with _Slot(slow):
        try:
            # no "-ExecutionPolicy Bypass": the policy never applies to a -Command string, and the Windows modules
            # these scripts use load under any policy; on a command line it is what malware droppers look like
            p = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                                  "-Command", _PRE + script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except FileNotFoundError:
            raise _missing(["PowerShell"])
        try:
            out, _err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            try:
                p.communicate(timeout=5)
            except Exception:
                pass
            raise _timeout_error()
    return out.decode("utf-8", "replace")


def ps_json(script, timeout=None, as_list=False):
    """Runs `script | ConvertTo-Json` and returns the object. With as_list it always returns a list."""
    # strip(): in PowerShell 5 a line cannot start with "|"
    out = ps(f"{script.strip()} | ConvertTo-Json -Depth 6 -Compress", timeout=timeout).strip()
    if not out:
        return [] if as_list else None
    try:
        data = json.loads(out)
    except ValueError:
        return [] if as_list else None
    if as_list and not isinstance(data, list):
        data = [data]
    return data


def gather(*calls):
    """Runs independent reads at the same time (each `call` is a zero-argument function) and returns their results in
    order. Every call finishes before this returns; if some failed, the first exception is raised afterwards."""
    if len(calls) < 2:
        return [c() for c in calls]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(calls)) as ex:
        futs = [ex.submit(c) for c in calls]
    return [f.result() for f in futs]


def run(cmd, on_line=None, timeout=None, cwd=None, encoding=None, job=None, raise_on_timeout=False):
    """Runs a program. With on_line it gets each line as soon as it is printed (for live consoles).
    The output is read on its own thread, so a silent program cannot hold back Cancel or the timeout: if `job` is
    cancelled, or `timeout` seconds pass in total, the whole process tree is killed. A timeout returns what was
    read so far, or raises subprocess.TimeoutExpired with raise_on_timeout. Raises ToolError if the program is
    missing. Returns (exit code, full output)."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             cwd=cwd, creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise _missing(cmd)
    q = queue.Queue()

    def reader():
        try:
            for raw in iter(p.stdout.readline, b""):
                q.put(raw)
        except (OSError, ValueError):
            pass
        q.put(None)
    threading.Thread(target=reader, daemon=True).start()
    out = []
    enc = encoding or "utf-8"
    t0 = time.monotonic()
    ended = None
    timed_out = False
    while True:
        now = time.monotonic()
        if job is not None and job.cancelled:
            kill_tree(p)
            break
        if timeout and now - t0 > timeout:
            timed_out = True
            kill_tree(p)
            break
        try:
            raw = q.get(timeout=0.25)
        except queue.Empty:
            raw = b""
        if raw is None:
            break
        if raw:
            line = _decode(raw, enc)
            out.append(line)
            if on_line:
                on_line(line.rstrip("\r\n"))
        if ended is None and p.poll() is not None:
            ended = time.monotonic()
        if ended is not None and q.empty() and time.monotonic() - ended > 1.5:
            break  # the program is over and a child it started still holds the pipe
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        kill_tree(p)
    # lines the reader had queued before the kill
    while True:
        try:
            raw = q.get_nowait()
        except queue.Empty:
            break
        if raw:
            out.append(_decode(raw, enc))
    if timed_out and raise_on_timeout:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return p.returncode, "".join(out)


def _decode(raw, enc):
    # sfc/dism/chkdsk write UTF-16 or the OEM code page depending on the version
    if raw.count(b"\x00") > len(raw) // 3:
        return raw.decode("utf-16-le", "replace").replace("\x00", "")
    try:
        return raw.decode(enc)
    except UnicodeDecodeError:
        # the console code page of THIS Windows (850 in Western Europe, 437 in the US, 866 in Russian, 852 in Central
        # Europe...): a fixed cp850 garbled every accented letter on the others
        return raw.decode("oem", "replace")

"""RsHub's two on-demand scheduled tasks (open as administrator, and the updater), created from XML.

`schtasks /create /tr ...` leaves Windows' defaults, which broke RsHub on laptops and long sessions:
  * "Start the task only if the computer is on AC power": on battery the task stayed queued, so RsHub with
    "Always open as administrator" didn't open at all (and the updater never ran);
  * a 72-hour execution limit, after which Windows ends the program the task started;
  * priority 7 (below normal), which the program and everything it starts inherit.
The XML sets all of that explicitly. The tasks have no trigger: they only run when RsHub asks for them.
"""
import os
import subprocess
import tempfile
from xml.sax.saxutils import escape

NO_WINDOW = 0x08000000

_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>RsHub</Author>
    <Description>{desc}</Description>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>{multi}</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _user():
    """The account of this process's own token (DOMAIN, user). %USERDOMAIN% and %USERNAME% are only the fallback:
    they are the user's to change (HKCU Environment), and the tasks run with highest privileges as the account
    named here."""
    try:
        import ctypes
        buf, n = ctypes.create_unicode_buffer(512), ctypes.c_ulong(512)
        if ctypes.windll.secur32.GetUserNameExW(2, buf, ctypes.byref(n)) and buf.value:  # NameSamCompatible
            return buf.value
    except Exception:
        pass
    dom, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{dom}\\{name}" if dom and name else name


def task_xml(command, args="", desc="", multi="Parallel", limit="PT0S"):
    return _XML.format(command=escape(command), args=escape(args), desc=escape(desc), user=escape(_user()),
                       multi=multi, limit=limit)


def create(name, command, args="", desc="", multi="Parallel", limit="PT0S"):
    """Creates or replaces the task (needs administrator rights). True if it worked."""
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="rshub-task-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(task_xml(command, args, desc, multi, limit).encode("utf-16"))  # with BOM, as schtasks wants
        r = subprocess.run(["schtasks", "/create", "/tn", name, "/xml", path, "/f"], capture_output=True,
                           creationflags=NO_WINDOW, timeout=30)  # a stuck Task Scheduler must not hold the updater
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def query_xml(name):
    """The task's definition as text, or '' if it doesn't exist."""
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", name, "/xml"], capture_output=True, timeout=10,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    raw = r.stdout
    return raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("oem", "replace")


def outdated(xml):
    """True for a task made the old way (it doesn't start on battery, or Windows ends it after a while)."""
    if not xml:
        return False
    return ("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" not in xml
            or "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" not in xml
            or "<Priority>4</Priority>" not in xml)


def end(name):
    """Stops (or drops from the queue) a running instance of the task."""
    try:
        subprocess.run(["schtasks", "/end", "/tn", name], capture_output=True, timeout=10, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass

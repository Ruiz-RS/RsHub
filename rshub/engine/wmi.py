"""WMI (CIM) queries inside RsHub's own process, without starting powershell.exe.

The app already runs on pythonnet (the window and the tray use .NET through it), so System.Management, the .NET
Framework's WMI client, is one `clr.AddReference` away. A Get-CimInstance in a hidden PowerShell costs a process, about
0.5 s of start-up and a pattern antivirus products watch for; the same query here takes a few milliseconds.

    rows = query(r"root\\cimv2", "Win32_ComputerSystem", ["Manufacturer", "Model"])        -> [{"Manufacturer": ...}, ...]
    rows = query(r"root\\cimv2", "Win32_PnPEntity", ["Name"], "ConfigManagerErrorCode<>0")

Values come back as plain Python: numbers, str, bool, None, and lists for arrays (a .NET UInt16[] becomes a list of
ints). A CIM date stays the text WMI gives; use cim_datetime() to read it. Anything that goes wrong (no pythonnet, a
missing class or namespace, access denied, a timeout) raises WmiError, so a caller can fall back to PowerShell."""
import datetime
import re
import threading

TIMEOUT_S = 40


class WmiError(Exception):
    pass


_lock = threading.Lock()
_net = None


def _dotnet():
    """The System.Management types, loaded once (the first call costs ~0.3 s)."""
    global _net
    with _lock:
        if _net is None:
            try:
                import clr  # noqa: F401  (pythonnet)
                clr.AddReference("System.Management")
                from System import TimeSpan
                from System.Management import EnumerationOptions, ManagementObjectSearcher, ManagementScope, ObjectQuery
                _net = (TimeSpan, EnumerationOptions, ManagementObjectSearcher, ManagementScope, ObjectQuery)
            except Exception as e:
                raise WmiError(f"System.Management is not available: {e}") from e
        return _net


def _py(v):
    """A WMI property value as a plain Python value."""
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    try:
        return [_py(x) for x in v]  # a .NET array
    except TypeError:
        return str(v)


def query(namespace, cls, props=None, where=None, timeout=TIMEOUT_S):
    """Rows of `cls` in `namespace` as dicts of the requested properties (all of them if props is None)."""
    TimeSpan, EnumerationOptions, ManagementObjectSearcher, ManagementScope, ObjectQuery = _dotnet()
    text = "SELECT " + (", ".join(props) if props else "*") + " FROM " + cls + (" WHERE " + where if where else "")
    try:
        opts = EnumerationOptions()
        opts.Timeout = TimeSpan.FromSeconds(timeout)
        opts.ReturnImmediately = False
        opts.Rewindable = False
        searcher = ManagementObjectSearcher(ManagementScope(namespace), ObjectQuery(text), opts)
        out = []
        for obj in searcher.Get():
            try:
                names = list(props) if props else [p.Name for p in obj.Properties]
                out.append({n: _py(obj[n]) for n in names})
            finally:
                obj.Dispose()
        return out
    except WmiError:
        raise
    except Exception as e:  # System.Management.ManagementException, access denied, timeout...
        raise WmiError(f"{namespace}: {text}: {e}") from e


def cim_datetime(value):
    """A CIM date ("20251118102133.000000-300") as a local naive datetime, or None. Like .NET's
    ManagementDateTimeConverter: a time with a UTC offset is converted to local time, one whose offset or fraction is
    all asterisks ("20060621000000.******+***", what driver dates look like) is taken as it is."""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\.([\d*]{6})([+-])([\d*]{3})$", str(value or ""))
    if not m:
        return None
    y, mo, d, h, mi, s, us, sign, off = m.groups()
    try:
        naive = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), int(us.replace("*", "0")))
        if "*" in off:
            return naive
        utc = naive - datetime.timedelta(minutes=int(off) * (1 if sign == "+" else -1))
        return utc.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None

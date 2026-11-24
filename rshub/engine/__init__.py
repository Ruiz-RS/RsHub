"""RsHub engine: the part that actually touches Windows. No UI.

Each module is a file rshub/engine/<name>.py that declares:
    EXPORTS = {"function", ...}  functions the UI can call that answer quickly (< ~2 s)
    JOBS = {"function", ...}     long functions: they run as a task, get `job` as their first argument
                                 and report progress with job.progress(...) / job.log(...)
The UI calls them with SH.engine('<module>', '<function>', ...args) or SH.job('<module>', '<function>', args, options).
Everything they return must be JSON-serializable.
"""
import importlib

_cache = {}


def module(name):
    if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
        raise ValueError(f"invalid module: {name!r}")
    if name not in _cache:
        _cache[name] = importlib.import_module(f"{__name__}.{name}")
    return _cache[name]


def resolve(mod, fn, kind):
    m = module(mod)
    allowed = getattr(m, "JOBS" if kind == "job" else "EXPORTS", set())
    if fn not in allowed:
        raise PermissionError(f"{mod}.{fn} is not allowed as a {kind}")
    return getattr(m, fn)

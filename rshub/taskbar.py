"""Progress on RsHub's taskbar button (ITaskbarList3), while long work the user started runs (SH.activity in
core.js sends the aggregate: Api.taskbar_progress(state, value)).

Everything happens on one worker thread that owns the COM object (created in its own single-threaded apartment):
callers only drop the latest wish and return, so the window thread and the page never wait for it. Only the newest
wish counts (a burst of progress updates becomes one call). Any failure (no taskbar, COM refused, the window hidden in
the tray with no button) skips that update; repeated failures, or a COM object that cannot be created at all, turn the
feature off for the session. Nothing here may raise into the caller."""
import ctypes
import threading

# TBPFLAG
STATES = {"none": 0, "indeterminate": 1, "normal": 2, "error": 4, "paused": 8}
_CLSID_TASKBARLIST = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_IID_ITASKBARLIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
_SCALE = 1000          # SetProgressValue(completed, total): thousandths
_MAX_FAILS = 6


def normalize(state, value=0.0):
    """(state, value) as the worker applies it: an unknown state is "none", the value a number in 0..1 (only the
    states that show a bar keep it)."""
    st = state if state in STATES else "none"
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    if v != v:  # NaN
        v = 0.0
    v = max(0.0, min(1.0, v))
    if st in ("none", "indeterminate"):
        v = 0.0
    return st, round(v, 3)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class ComTaskbar:
    """ITaskbarList3 through raw COM (no comtypes): HrInit, SetProgressValue, SetProgressState. Create and use it on
    one thread only."""
    def __init__(self):
        ole32 = ctypes.WinDLL("ole32")
        self._ole32 = ole32
        ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED; S_FALSE (already) is fine
        clsid, iid = _GUID(), _GUID()
        if ole32.CLSIDFromString(ctypes.c_wchar_p(_CLSID_TASKBARLIST), ctypes.byref(clsid)) or \
                ole32.CLSIDFromString(ctypes.c_wchar_p(_IID_ITASKBARLIST3), ctypes.byref(iid)):
            raise OSError("bad GUID")
        p = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(p))  # CLSCTX_INPROC_SERVER
        if hr != 0 or not p.value:
            raise OSError(f"CoCreateInstance: {hr & 0xFFFFFFFF:#x}")
        self._p = p
        self._vt = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        self._call(3)  # HrInit

    def _call(self, index, *args, argtypes=()):
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        return proto(self._vt[index])(self._p, *args)  # a failing HRESULT raises OSError

    def apply(self, hwnd, state, value):
        h = ctypes.c_void_p(hwnd)
        self._call(10, h, STATES[state], argtypes=(ctypes.c_void_p, ctypes.c_int))  # SetProgressState
        if state in ("normal", "error", "paused"):
            self._call(9, h, ctypes.c_ulonglong(int(round(value * _SCALE))), ctypes.c_ulonglong(_SCALE),
                       argtypes=(ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulonglong))  # SetProgressValue

    def release(self):
        try:
            self._call(2)  # Release
        except Exception:
            pass
        try:
            self._ole32.CoUninitialize()
        except Exception:
            pass


class TaskbarProgress:
    """hwnd_fn(): the main window's handle, or None while it is not known (then the wish waits for reapply()).
    factory(): the COM wrapper (ComTaskbar; tests pass a fake)."""
    def __init__(self, hwnd_fn, factory=ComTaskbar):
        self._hwnd_fn = hwnd_fn
        self._factory = factory
        self._cv = threading.Condition()
        self._want = ("none", 0.0)
        self._have = None
        self._gen = 0
        self._thread = None
        self._stop = False
        self.dead = False
        self.fails = 0

    def set(self, state, value=0.0):
        """Records the wish and returns at once: True if it will be applied, False when the feature is off."""
        if self.dead or self._stop:
            return False
        want = normalize(state, value)
        with self._cv:
            self._want = want
            self._gen += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="taskbar-progress")
                self._thread.start()
            self._cv.notify()
        return True

    def reapply(self):
        """The taskbar button was made again (the window came back from the tray): send the current state again."""
        if self.dead or self._thread is None:
            return
        with self._cv:
            self._have = None
            self._gen += 1
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()

    def _run(self):
        try:
            com = self._factory()
        except Exception as e:
            print("taskbar progress: not available:", e, flush=True)
            self.dead = True
            return
        seen = -1
        try:
            while True:
                with self._cv:
                    while self._gen == seen and not self._stop:
                        self._cv.wait()
                    if self._stop:
                        return
                    seen, want = self._gen, self._want
                if want == self._have:
                    continue
                try:
                    hwnd = self._hwnd_fn()
                except Exception:
                    hwnd = None
                if not hwnd:
                    continue
                try:
                    com.apply(hwnd, *want)
                    self._have = want
                    self.fails = 0
                except Exception as e:
                    self.fails += 1
                    if self.fails >= _MAX_FAILS:
                        print("taskbar progress: turned off after repeated failures:", e, flush=True)
                        self.dead = True
                        return
        finally:
            try:
                com.release()
            except Exception:
                pass

"""Long tasks with progress, a log and cancellation. Updates reach the UI as SH.emit('job', {...}).

    def scan(job, folder):
        for i, f in enumerate(files):
            if job.cancelled: return job.stop()        # the user pressed Cancel
            job.progress(i / total, f"Reading {f}", files=i)   # pct 0..1, text, extra data
            job.log("output line")                     # for live consoles (SFC, DISM, winget...)
        return result                                  # arrives as {state:'done', result}
"""
import itertools
import threading
import time
import traceback

_emit = lambda event, data: None  # the app replaces it with the sender to the UI
_jobs = {}
_ids = itertools.count(1)


def set_emitter(fn):
    global _emit
    _emit = fn


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, name):
        self.id = f"j{next(_ids)}"
        self.name = name
        self.cancelled = False
        self.state = "running"
        self.pct = 0.0
        self.text = ""
        self.data = {}
        self.result = None
        self.ended = 0.0
        self._last = 0.0
        self._logbuf = []
        self._lock = threading.Lock()

    # ---- for the task code ----
    def progress(self, pct=None, text=None, force=False, **data):
        if pct is not None:
            self.pct = max(0.0, min(1.0, float(pct)))
        if text is not None:
            self.text = str(text)
        self.data.update(data)
        now = time.monotonic()
        if force or now - self._last > 0.1 or self.pct >= 1:
            self._last = now
            self._send("progress")

    def log(self, line):
        with self._lock:
            self._logbuf.append(str(line).rstrip("\r\n"))
        if time.monotonic() - self._last > 0.1:
            self._last = time.monotonic()
            self._send("progress")

    def check(self):
        """Raises Cancelled if the user cancelled (useful inside deep loops)."""
        if self.cancelled:
            raise Cancelled()

    def stop(self):
        raise Cancelled()

    # ---- internal ----
    def _send(self, kind):
        with self._lock:
            logs, self._logbuf = self._logbuf, []
        msg = {"id": self.id, "name": self.name, "kind": kind, "state": self.state, "pct": self.pct,
               "text": self.text, "data": self.data}
        if logs:
            msg["log"] = logs
        if kind == "end":
            msg["result"] = self.result
        try:
            _emit("job", msg)
        except Exception as e:
            print(f"[job {self.name}] could not send {kind}: {e}", flush=True)
            if kind == "end":  # e.g. a result that is not JSON: the UI must still hear that the task is over
                msg.update(state="error", result={"error": f"The task finished but its result could not be delivered: {e}"})
                try:
                    _emit("job", msg)
                except Exception:
                    pass

    def info(self):
        return {"id": self.id, "name": self.name, "state": self.state, "pct": self.pct, "text": self.text}


KEEP_FINISHED = 600  # seconds a finished job stays cancellable/listed; older ones are dropped when the next starts


def _prune():
    now = time.monotonic()
    for k in [k for k, j in list(_jobs.items()) if j.ended and now - j.ended > KEEP_FINISHED]:
        _jobs.pop(k, None)


def start(name, fn, *args, **kwargs):
    _prune()
    job = Job(name)
    _jobs[job.id] = job

    def run():
        try:
            job.result = fn(job, *args, **kwargs)
            job.state = "done"
        except Cancelled:
            job.state = "cancelled"
        except BaseException as e:  # also SystemExit and the like: the UI must always get an "end"
            job.state = "error"
            job.result = {"error": str(e), "trace": traceback.format_exc(limit=6)}
            print(f"[job {name}]", traceback.format_exc())
        job.pct = 1.0 if job.state == "done" else job.pct
        job._send("end")
        job.result = None   # it was delivered with the "end" message: a big scan result must not stay in memory
        job.ended = time.monotonic()

    threading.Thread(target=run, daemon=True, name=f"job-{name}").start()
    return job.id


def cancel(job_id):
    j = _jobs.get(job_id)
    if j:
        j.cancelled = True
        return True
    return False


def running():
    # a copy: a task started on another thread meanwhile would change the dict while it is read ("dictionary changed
    # size during iteration"), and asking to quit then went ahead without saying that work was still running
    return [j.info() for j in list(_jobs.values()) if j.state == "running"]

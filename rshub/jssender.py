"""Runs a window's evaluate_js calls off the window thread, in order."""
import collections
import threading
import time


class Runner:
    """Runs evaluate_js calls on its own thread and waits at most `timeout` seconds for each. pywebview's evaluate_js
    waits for WebView2's reply with no time limit; if a reply never comes (a script WebView2 could not run, a page
    being replaced) that thread would wait forever, and everything queued behind it with it. After the timeout the
    stuck thread is left behind (it costs a parked thread) and a fresh one takes the next call."""
    def __init__(self, window, name, timeout=15.0):
        self.window, self.name, self.timeout = window, name, timeout
        self._stopped = False
        self._new()

    def _new(self):
        self._job, self._done = None, threading.Event()
        self._go = threading.Event()
        go, box = self._go, self

        def work():
            while True:
                go.wait()
                go.clear()
                if box._stopped:
                    return
                code, done = box._job
                try:
                    box.window.evaluate_js(code)
                except Exception:
                    pass
                done.set()
                if box._go is not go:  # this thread was given up on while it waited: a newer one took over
                    return
        threading.Thread(target=work, daemon=True, name=self.name).start()

    def stop(self):
        """The window is gone: its thread ends (one parked thread per closed widget was left behind before)."""
        self._stopped = True
        self._go.set()

    def run(self, code):
        """True if the page answered in time."""
        if self._stopped:
            return False
        done = threading.Event()
        self._job = (code, done)
        self._go.set()
        if done.wait(self.timeout):
            return True
        print(f"[js] the page did not answer an evaluate_js in {self.timeout:.0f} s; going on without it", flush=True)
        self._new()
        return False


class Sender:
    """One thread per window that runs its evaluate_js calls IN ORDER from a bounded queue. evaluate_js waits for
    WebView2's reply, which arrives on the window thread, so nobody on that thread may call it directly: they put the
    code here instead. Under pressure the oldest "lossy" item (job progress) goes first, never an "end"."""
    LIMIT = 600

    def __init__(self, window):
        self.window = window
        self._q = collections.deque()
        self._cv = threading.Condition()
        self._stop = False
        self._since = 0.0  # when the evaluate_js in progress started (0: none), for the freeze detector
        self._runner = Runner(window, "js-sender-run")
        threading.Thread(target=self._run, daemon=True, name="js-sender").start()

    def busy_for(self):
        """Seconds the evaluate_js in progress has been waiting for the page (0 when idle)."""
        s = self._since
        return time.monotonic() - s if s else 0.0

    def put(self, code, lossy=False):
        with self._cv:
            if self._stop:
                return
            if len(self._q) >= self.LIMIT:
                for i, (_, l) in enumerate(self._q):
                    if l:
                        del self._q[i]
                        break
                else:
                    self._q.popleft()
            self._q.append((code, lossy))
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stop = True
            self._q.clear()
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                code, _ = self._q.popleft()
            self._since = time.monotonic()
            try:
                self._runner.run(code)
            finally:
                self._since = 0.0

import threading
import time
import unittest

from rshub.jssender import Sender as _Sender


class FakeWindow:
    def __init__(self, gate=None):
        self.calls, self.gate, self.threads = [], gate, set()

    def evaluate_js(self, code):
        self.threads.add(threading.get_ident())
        if self.gate is not None and not self.calls:
            self.gate.wait(5)
        self.calls.append(code)


def wait_for(cond, secs=3):
    end = time.time() + secs
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


class SenderTests(unittest.TestCase):
    def test_order_and_single_thread(self):
        w = FakeWindow()
        s = _Sender(w)
        for i in range(200):
            s.put(str(i), lossy=i % 2 == 0)
        self.assertTrue(wait_for(lambda: len(w.calls) == 200))
        self.assertEqual(w.calls, [str(i) for i in range(200)])
        self.assertEqual(len(w.threads), 1)
        s.stop()

    def test_bounded_drops_lossy_first_and_keeps_end(self):
        gate = threading.Event()
        w = FakeWindow(gate)
        s = _Sender(w)
        s.put("first")
        time.sleep(0.02)  # the sender is now busy with "first"
        for i in range(_Sender.LIMIT + 50):
            s.put(f"p{i}", lossy=True)
        s.put("end")
        gate.set()
        self.assertTrue(wait_for(lambda: "end" in w.calls))
        self.assertLessEqual(len(w.calls), _Sender.LIMIT + 2)
        self.assertEqual(w.calls[-1], "end")
        s.stop()

    def test_stop_ends_thread(self):
        w = FakeWindow()
        s = _Sender(w)
        s.stop()
        s.put("x")
        time.sleep(0.1)
        self.assertEqual(w.calls, [])


class TelemetryRunnerTests(unittest.TestCase):
    def test_a_closed_windows_runner_and_thread_go_away(self):
        """Each window gets a telemetry runner with a thread of its own; closing the widget left both behind (one
        thread and one dead window per open/close)."""
        from rshub.api import Api
        api = Api()
        w = FakeWindow()
        r = api._tele_runner(w)
        self.assertTrue(r.run("sample"))
        names = lambda: [t for t in threading.enumerate() if t.name == "tele-js"]
        before = len(names())
        api._sender_drop(w)
        self.assertNotIn(w, api._tele_runners)
        self.assertTrue(wait_for(lambda: len(names()) == before - 1))
        self.assertFalse(r.run("late sample"))  # a sample already on its way is dropped, no new thread
        self.assertEqual(len(names()), before - 1)
        self.assertEqual(w.calls, ["sample"])


if __name__ == "__main__":
    unittest.main()


class PanelGeometryTests(unittest.TestCase):
    def test_edges_and_scale(self):
        from rshub.winmgr import panel_geometry as g
        mon = (0, 0, 1920, 1080)
        # taskbar at the bottom, 150% system scale: sized with k, above the taskbar, centred on the click
        x, y, w, h = g((1800, 1060), (0, 0, 1920, 1040), mon, 1.5, 344, 452)
        self.assertEqual((w, h), (516, 678))
        self.assertEqual(y + h, 1040 - 15)
        self.assertLessEqual(x + w, 1920 - 15)
        # top
        x, y, w, h = g((960, 10), (0, 40, 1920, 1080), mon, 1.0, 344, 452)
        self.assertEqual(y, 50)
        # left taskbar
        x, y, w, h = g((10, 500), (60, 0, 1920, 1080), mon, 1.0, 344, 452)
        self.assertEqual(x, 70)
        # right taskbar on a secondary monitor with negative coordinates
        x, y, w, h = g((-10, 500), (-1920, 0, -60, 1080), (-1920, 0, 0, 1080), 1.25, 344, 452)
        self.assertEqual(x + w, -60 - 12)
        # auto-hidden taskbar: nearest edge to the click
        x, y, w, h = g((500, 1075), mon, mon, 1.0, 344, 452)
        self.assertEqual(y + h, 1080 - 10)
        # always inside the work area
        for pt in ((0, 0), (1919, 1079), (960, 540)):
            x, y, w, h = g(pt, (0, 0, 1920, 1040), mon, 2.0, 344, 452)
            self.assertTrue(10 * 2 <= x and x + w <= 1920 - 20 and 20 <= y and y + h <= 1040 - 20)

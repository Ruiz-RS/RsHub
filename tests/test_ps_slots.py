"""PowerShell slots: slow reads may hold at most 2 of the 5 slots, quick reads are never left behind them, and
nobody waits for a slot forever."""
import threading
import time
import unittest
from unittest import mock

from rshub.engine import ps


class SlotTests(unittest.TestCase):
    def test_slow_reads_leave_room_for_quick_ones(self):
        release = threading.Event()
        inside = []

        def slow():
            with ps._Slot(True):
                inside.append("slow")
                release.wait(5)
        threads = [threading.Thread(target=slow) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        self.assertEqual(inside.count("slow"), 2)       # only two slow reads at a time
        t0 = time.monotonic()
        with ps._Slot(False):                              # a quick read still gets a slot at once
            self.assertLess(time.monotonic() - t0, 0.5)
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(inside.count("slow"), 4)

    def test_waiting_for_a_slot_has_a_limit(self):
        with mock.patch.object(ps, "SLOT_WAIT", 0.2), mock.patch.object(ps, "_SLOTS", threading.BoundedSemaphore(1)):
            with ps._Slot(False):
                t0 = time.monotonic()
                with self.assertRaises(ps.ToolError):
                    with ps._Slot(False):
                        pass
                self.assertLess(time.monotonic() - t0, 1.5)

    def test_a_failed_wait_gives_back_what_it_took(self):
        slots, slow = threading.BoundedSemaphore(1), threading.BoundedSemaphore(2)
        with mock.patch.object(ps, "SLOT_WAIT", 0.1), mock.patch.object(ps, "_SLOTS", slots), \
                mock.patch.object(ps, "_SLOW", slow):
            with ps._Slot(False):
                with self.assertRaises(ps.ToolError):
                    with ps._Slot(True):
                        pass
            self.assertTrue(slow.acquire(blocking=False) and slow.acquire(blocking=False))  # both slow gates free


if __name__ == "__main__":
    unittest.main()

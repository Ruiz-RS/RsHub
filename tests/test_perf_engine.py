"""Caching and parallel-read logic added for speed: results must stay the same, just sooner."""
import threading
import time
import unittest
from unittest import mock

from rshub.engine import drivers, jobs, optimizer, ps, system


class GatherTests(unittest.TestCase):
    def test_results_keep_the_order_of_the_calls(self):
        slow_first = [lambda: (time.sleep(0.15), "a")[1], lambda: "b", lambda: "c"]
        self.assertEqual(ps.gather(*slow_first), ["a", "b", "c"])

    def test_runs_at_the_same_time(self):
        t0 = time.monotonic()
        ps.gather(*[lambda: time.sleep(0.3) for _ in range(4)])
        self.assertLess(time.monotonic() - t0, 0.9)

    def test_first_error_is_raised_after_all_finished(self):
        done = []

        def bad():
            raise ValueError("x")

        def good():
            time.sleep(0.1)
            done.append(1)
        with self.assertRaises(ValueError):
            ps.gather(bad, good)
        self.assertEqual(done, [1])

    def test_single_call_has_no_threads(self):
        self.assertEqual(ps.gather(lambda: threading.current_thread().name), [threading.current_thread().name])


class DriverCacheTests(unittest.TestCase):
    def setUp(self):
        drivers._cache.update(drivers=None, at=0)

    def tearDown(self):
        drivers._cache.update(drivers=None, at=0)

    def test_max_age_shares_one_measurement(self):
        rows = [{"n": "A", "c": "NET", "p": "Intel", "v": "1", "d": "2024-01-01", "id": "X1", "e": 0}]
        with mock.patch.object(drivers, "_inventory", return_value=rows) as q:
            first = drivers.list_drivers()
            drivers._cache["at"] -= 300           # older than the 120 s of the Drivers page, younger than 600 s
            self.assertEqual(len(drivers.list_drivers(max_age=600)), 1)
            self.assertEqual(q.call_count, 1)
            drivers.list_drivers()                 # the Drivers page wants it newer: asks again
            self.assertEqual(q.call_count, 2)
            drivers.list_drivers(fresh=True)
            self.assertEqual(q.call_count, 3)
        self.assertEqual(first[0]["name"], "A")

    def test_count_comes_from_the_inventory(self):
        rows = [{"n": "A", "v": "1", "id": "1"}, {"n": "B", "v": "2", "id": "2"}]
        with mock.patch.object(drivers, "_inventory", return_value=rows):
            self.assertEqual(system._drivers_count(), 2)


class SystemQueryTests(unittest.TestCase):
    """pc_info reads WMI inside the process; the three PowerShell reads are only the fallback."""

    def test_wmi_is_used_first_and_no_powershell_starts(self):
        with mock.patch.object(system, "_query_wmi", lambda: {"maker": "ACME"}), mock.patch.object(system, "_sysdisk_wmi", lambda: 0),                 mock.patch.object(system, "_nets_wmi", lambda: [{"name": "Wi-Fi"}]),                 mock.patch.object(system, "ps_json", side_effect=AssertionError("powershell started")):
            r = system._query()
        self.assertEqual((r["maker"], r["sysDisk"], r["nets"]), ("ACME", 0, [{"name": "Wi-Fi"}]))

    def test_powershell_is_the_fallback_when_wmi_fails(self):
        def boom():
            raise system.wmi.WmiError("no System.Management")
        with mock.patch.object(system, "_query_wmi", boom), mock.patch.object(system, "_query_ps", lambda: {"maker": "PS"}):
            self.assertEqual(system._query()["maker"], "PS")
        with mock.patch.object(system.wmi, "query", side_effect=system.wmi.WmiError("x")),                 mock.patch.object(drivers, "ps_json", return_value=[{"n": "A", "v": "1", "id": "1"}]):
            self.assertEqual(len(drivers._inventory()), 1)

    def test_link_speed_reads_like_get_netadapter(self):
        self.assertEqual([system._link_speed(v) for v in (1201000000, 1000000000, 866700000, 100000000, 2500000000, 0)],
                         ["1.2 Gbps", "1 Gbps", "866.7 Mbps", "100 Mbps", "2.5 Gbps", "0 bps"])

    def test_cim_dates(self):
        from rshub.engine import wmi
        self.assertEqual(wmi.cim_datetime("20060621000000.******+***").strftime("%Y-%m-%d"), "2006-06-21")  # a driver date
        self.assertIsNone(wmi.cim_datetime("garbage"))
        self.assertIsNone(wmi.cim_datetime(None))

    def test_merges_the_three_reads(self):
        def fake(script, timeout=90, as_list=False):
            if script == system.SCRIPT_SYSDISK:
                return 0
            if script == system.SCRIPT_NETS:
                return [{"name": "Wi-Fi"}]
            return {"maker": "ACME"}
        with mock.patch.object(system, "ps_json", fake):
            r = system._query_ps()
        self.assertEqual((r["maker"], r["sysDisk"], r["nets"]), ("ACME", 0, [{"name": "Wi-Fi"}]))

    def test_a_failing_side_read_does_not_lose_the_rest(self):
        def fake(script, timeout=90, as_list=False):
            if script in (system.SCRIPT_SYSDISK, system.SCRIPT_NETS):
                raise ps.ToolError("slow")
            return {"maker": "ACME"}
        with mock.patch.object(system, "ps_json", fake):
            r = system._query_ps()
        self.assertEqual((r["maker"], r["sysDisk"], r["nets"]), ("ACME", None, []))


class ServiceRowTests(unittest.TestCase):
    def test_rows_use_the_win32_words(self):
        import psutil

        class Svc:
            def __init__(self, d):
                self.d = d

            def as_dict(self):
                return self.d
        table = {"A": {"name": "A", "status": "running", "start_type": "automatic"},
                 "B": {"name": "B", "status": "stopped", "start_type": "disabled"}}

        def get(n):
            if n not in table:
                raise psutil.NoSuchProcess(0, n)
            return Svc(table[n])
        with mock.patch("psutil.win_service_get", get):
            rows = optimizer._service_rows(["A", "B", "Z"])
        self.assertEqual(rows, {"A": {"Name": "A", "State": "Running", "StartMode": "Auto"},
                                "B": {"Name": "B", "State": "Stopped", "StartMode": "Disabled"}})

    def test_printers_fall_back_to_windows_when_the_api_is_unsure(self):
        with mock.patch.object(optimizer, "_printer_names", return_value=None), \
                mock.patch.object(optimizer, "ps_json", return_value=[{"Name": "Laser"}, {"Name": "Microsoft Print to PDF"}]):
            self.assertEqual(optimizer._printers(), ["Laser"])

    def test_virtual_printers_are_ignored(self):
        with mock.patch.object(optimizer, "_printer_names", return_value=["OneNote", "HP LaserJet"]):
            self.assertEqual(optimizer._printers(), ["HP LaserJet"])


class JobMemoryTests(unittest.TestCase):
    def test_finished_job_drops_its_result_but_delivers_it(self):
        got = []
        old = jobs._emit
        jobs.set_emitter(lambda ev, msg: got.append(msg))
        try:
            jid = jobs.start("t", lambda job: {"big": "x" * 1000})
            for _ in range(100):
                if jobs._jobs[jid].ended:
                    break
                time.sleep(0.02)
        finally:
            jobs.set_emitter(old)
        self.assertEqual(got[-1]["result"], {"big": "x" * 1000})
        self.assertIsNone(jobs._jobs[jid].result)

    def test_old_finished_jobs_are_pruned_on_the_next_start(self):
        j = jobs.Job("old")
        j.ended = time.monotonic() - jobs.KEEP_FINISHED - 1
        jobs._jobs[j.id] = j
        jobs._prune()
        self.assertNotIn(j.id, jobs._jobs)


if __name__ == "__main__":
    unittest.main()

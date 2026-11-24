"""Privacy & Security engine: pure helpers and the backup/undo path, against an in-memory registry.
Nothing here reads or writes the real registry, firewall, tasks or services."""
import shutil
import tempfile
import time
import unittest
import winreg
from unittest import mock

from rshub.engine import privacy as P


class FakeReg:
    """Same interface as privacy.WinReg, backed by a dict."""

    def __init__(self, data=None, fail_on=None):
        self.d = dict(data or {})
        self.fail_on = fail_on

    def _k(self, hive, path, name):
        return (hive, path.lower(), name.lower())

    def read(self, hive, path, name):
        k = self._k(hive, path, name)
        if k in self.d:
            data, typ = self.d[k]
            return True, data, typ
        return False, None, None

    def write(self, hive, path, name, typ, data):
        if self.fail_on and name == self.fail_on:
            raise OSError("access denied")
        self.d[self._k(hive, path, name)] = (data, typ)

    def delete(self, hive, path, name):
        self.d.pop(self._k(hive, path, name), None)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [mock.patch.object(P, "DATA", self.tmp), mock.patch.object(P, "_admin", lambda: False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Filetime(unittest.TestCase):
    def test_known_value(self):
        # 2025-01-01T00:00:00Z
        self.assertAlmostEqual(P.filetime_to_unix(133801632000000000), 1735689600, places=3)

    def test_zero_missing_garbage(self):
        for v in (0, None, "x", -5, 2 ** 63):
            self.assertIsNone(P.filetime_to_unix(v))

    def test_binary_qword(self):
        ft = 133801632000000000
        self.assertAlmostEqual(P.filetime_to_unix(ft.to_bytes(8, "little")), 1735689600, places=3)

    def test_in_use_now(self):
        self.assertTrue(P.usage(133801632000000000, 0)["active"])
        self.assertFalse(P.usage(133801632000000000, 133801633000000000)["active"])
        self.assertFalse(P.usage(None, 0)["active"])
        self.assertFalse(P.usage(133801632000000000, None)["active"])


class Score(unittest.TestCase):
    def test_score(self):
        self.assertEqual(P.score([]), 100)
        self.assertEqual(P.score([{"w": 6}, {"w": 4}, {"w": 0}]), 90)
        self.assertEqual(P.score([{"w": 80}, {"w": 80}]), 5)
        self.assertEqual(P.score([{"w": None}, {"w": -3}]), 100)

    def test_sort(self):
        fs = [{"sev": "info", "w": 9}, {"sev": "bad", "w": 1}, {"sev": "warn", "w": 2}, {"sev": "bad", "w": 5}]
        self.assertEqual([(f["sev"], f["w"]) for f in P.sort_findings(fs)], [("bad", 5), ("bad", 1), ("warn", 2), ("info", 9)])


class Decoders(unittest.TestCase):
    def test_product_state(self):
        self.assertEqual(P.product_state(397568), (True, True))    # 0x061100 Defender on, up to date
        self.assertEqual(P.product_state(393472), (False, True))   # 0x060100 Defender passive
        self.assertEqual(P.product_state(397584), (True, False))   # 0x061110 out of date
        self.assertEqual(P.product_state(266240), (True, True))    # 0x041000 third-party on
        self.assertEqual(P.product_state(None), (None, None))

    def test_uac(self):
        self.assertEqual(P.uac_level(0, 5, 1), "off")
        self.assertEqual(P.uac_level(1, 0, 0), "never")
        self.assertEqual(P.uac_level(1, 5, 1), "default")
        self.assertEqual(P.uac_level(1, 5, 0), "nodim")
        self.assertEqual(P.uac_level(1, 2, 1), "always")
        self.assertEqual(P.uac_level(1, 3, 1), "custom")

    def test_wifi(self):
        self.assertEqual(P.wifi_level("Open"), "open")
        self.assertEqual(P.wifi_level("WEP", "WEP"), "weak")
        self.assertEqual(P.wifi_level("WPA2-Personal", "TKIP"), "weak")
        self.assertEqual(P.wifi_level("WPA2-Personal", "AES-CCMP"), "good")
        self.assertEqual(P.wifi_level("WPA3-Personal", "GCMP"), "good")

    def test_scope(self):
        self.assertEqual(P.ip_scope("0.0.0.0"), "all")
        self.assertEqual(P.ip_scope("::"), "all")
        self.assertEqual(P.ip_scope("127.0.0.1"), "local")
        self.assertEqual(P.ip_scope("::1"), "local")
        self.assertEqual(P.ip_scope("192.168.1.20"), "lan")


class Hosts(unittest.TestCase):
    TEXT = """# comment
127.0.0.1 localhost
0.0.0.0 ads.tracker.example   # ad block
0.0.0.0 update.microsoft.com
10.1.2.3 www.paypal.com paypal.com
192.168.1.5 nas.home
"""

    def test_parse(self):
        e = P.parse_hosts(self.TEXT)
        self.assertEqual([x["host"] for x in e], ["localhost", "ads.tracker.example", "update.microsoft.com", "www.paypal.com", "paypal.com", "nas.home"])
        self.assertEqual(e[3]["line"], 5)

    def test_classify(self):
        c = {x["host"]: P.classify_host(x) for x in P.parse_hosts(self.TEXT)}
        self.assertEqual(c["localhost"][0], "local")
        self.assertEqual(c["ads.tracker.example"], ("block", "info"))
        self.assertEqual(c["update.microsoft.com"], ("block", "warn"))
        self.assertEqual(c["www.paypal.com"], ("redirect", "bad"))
        self.assertEqual(c["nas.home"], ("redirect", "info"))


class Risk(unittest.TestCase):
    def test_signed_program(self):
        self.assertEqual(P.assess(r"C:\Program Files\Vendor\app.exe", "", "Valid", "Vendor Inc.", "Vendor")[0], "low")

    def test_signed_in_appdata_is_fine(self):
        self.assertEqual(P.assess(r"C:\Users\u\AppData\Local\Discord\Update.exe", "", "Valid", "Discord Inc.", "")[0], "low")

    def test_unsigned_temp(self):
        risk, why = P.assess(r"C:\Users\u\AppData\Local\Temp\x.exe", "/s", "NotSigned", "", "")
        self.assertEqual(risk, "high")
        self.assertGreaterEqual(len(why), 3)

    def test_encoded_powershell(self):
        exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        self.assertEqual(P.assess(exe, "-w hidden -enc SQBFAFgA", "Valid", "Microsoft Windows", "")[0], "high")
        self.assertEqual(P.assess(exe, "-File C:\\scripts\\backup.ps1", "Valid", "Microsoft Windows", "")[0], "medium")

    def test_missing_file(self):
        self.assertEqual(P.assess(r"C:\Gone\old.exe", "", None, "", "", True)[0], "medium")

    def test_extensions(self):
        self.assertEqual(P.ext_risk(["storage", "alarms"], True)[0], "low")
        self.assertEqual(P.ext_risk(["<all_urls>", "webRequest"], True)[0], "medium")
        self.assertEqual(P.ext_risk(["<all_urls>", "cookies", "webRequest"], False)[0], "high")
        self.assertEqual(P.ext_risk(["storage"], True, overrides=True)[0], "medium")
        self.assertEqual(P.ext_risk(["https://*/*"], True)[0], "medium")


class Tracking(Sandbox):
    def test_state(self):
        adid = next(i for i in P.TRACKING if i["id"] == "adid")
        loc = next(i for i in P.TRACKING if i["id"] == "location")
        self.assertTrue(P.tracking_state(adid, FakeReg().read))  # missing -> Windows default (on)
        r = FakeReg({("HKCU", adid["vals"][0][1].lower(), "enabled"): (0, winreg.REG_DWORD)})
        self.assertFalse(P.tracking_state(adid, r.read))
        r = FakeReg({("HKCU", loc["vals"][0][1].lower(), "value"): ("Deny", winreg.REG_SZ)})
        self.assertFalse(P.tracking_state(loc, r.read))

    def test_turn_off_then_undo_restores_exactly(self):
        bing = next(i for i in P.TRACKING if i["id"] == "searchhist")
        tips = next(i for i in P.TRACKING if i["id"] == "tips")
        k_bing = ("HKCU", bing["vals"][0][1].lower(), "isdevicesearchhistoryenabled")
        k_tips = ("HKCU", tips["vals"][0][1].lower(), "subscribedcontent-338389enabled")
        fake = FakeReg({k_bing: (1, winreg.REG_DWORD), k_tips: (1, winreg.REG_DWORD)})
        before = dict(fake.d)
        with mock.patch.object(P, "REG", fake), mock.patch.object(P, "tracking_read", lambda: {"items": []}):
            r = P.tracking_set(["searchhist", "tips"], False)
            self.assertFalse(P.tracking_state(bing, fake.read))
            self.assertFalse(P.tracking_state(tips, fake.read))
            self.assertEqual(fake.d[k_bing][0], 0)
            hist = P.history()
            self.assertEqual(hist[0]["id"], r["backup"])
            self.assertFalse(hist[0]["undone"])
            P.undo(r["backup"])
            self.assertEqual(fake.d, before)  # values that did not exist are gone again; the others are back
            self.assertTrue(P.history()[0]["undone"])
            self.assertTrue(P.undo(r["backup"]).get("already"))

    def test_policies_under_hkcu_need_admin(self):
        # HKCU\Software\Policies is admin-only for an ordinary user: "bing" must not claim otherwise
        with mock.patch.object(P, "REG", FakeReg()), mock.patch.object(P, "_admin", return_value=False):
            self.assertEqual(P.tracking_set(["bing"], False), {"needAdmin": True})
            bing = next(i for i in P.tracking_read()["items"] if i["id"] == "bing")
            self.assertTrue(bing["admin"])
        self.assertTrue(P.user_needs_admin("HKCU", r"Software\Policies\Microsoft\Windows\Explorer"))
        self.assertFalse(P.user_needs_admin("HKCU", r"Software\Microsoft\Windows"))

    def test_admin_settings_need_admin(self):
        with mock.patch.object(P, "REG", FakeReg()):
            self.assertEqual(P.tracking_set(["diag"], False), {"needAdmin": True})

    def test_locked_by_policy(self):
        diag = next(i for i in P.TRACKING if i["id"] == "diag")
        lk = diag["lock"][0]
        with mock.patch.object(P, "REG", FakeReg({(lk[0], lk[1].lower(), lk[2].lower()): (0, winreg.REG_DWORD)})):
            with self.assertRaises(PermissionError):
                P.tracking_set(["diag"], False)

    def test_failed_write_rolls_back(self):
        fake = FakeReg({("HKCU", P._CDM.lower(), "subscribedcontent-338389enabled"): (1, winreg.REG_DWORD)}, fail_on="SoftLandingEnabled")
        before = dict(fake.d)
        with mock.patch.object(P, "REG", fake):
            with self.assertRaises(OSError):
                P.tracking_set(["tips"], False)
        self.assertEqual(fake.d, before)
        self.assertTrue(P.history()[0]["undone"])

    def test_refused_write_of_a_value_that_existed_still_puts_the_earlier_ones_back(self):
        # B existed and Windows refuses to write it: restoring it would be refused too, and that used to stop the
        # rollback before A, which stayed changed (also on every undo from the history)
        fake = FakeReg({("HKCU", "software\\x", "a"): (1, winreg.REG_DWORD), ("HKCU", "software\\x", "b"): (1, winreg.REG_DWORD)},
                       fail_on="B")
        before = dict(fake.d)
        with mock.patch.object(P, "REG", fake):
            with self.assertRaises(OSError):
                P.apply_writes([("HKCU", "Software\\X", "A", winreg.REG_DWORD, 0), ("HKCU", "Software\\X", "B", winreg.REG_DWORD, 0),
                                ("HKCU", "Software\\X", "C", winreg.REG_DWORD, 0)], "test")
        self.assertEqual(fake.d, before)
        self.assertTrue(P.history()[0]["undone"])

    def test_undo_goes_on_past_a_refused_value_and_can_be_tried_again(self):
        fake = FakeReg({("HKCU", "software\\x", "a"): (1, winreg.REG_DWORD), ("HKCU", "software\\x", "b"): (1, winreg.REG_DWORD)})
        with mock.patch.object(P, "REG", fake):
            bid = P.apply_writes([("HKCU", "Software\\X", "A", winreg.REG_DWORD, 0), ("HKCU", "Software\\X", "B", winreg.REG_DWORD, 0)], "test")
            fake.fail_on = "B"
            with self.assertRaises(OSError):
                P.undo(bid)
            self.assertEqual(fake.d[("HKCU", "software\\x", "a")][0], 1)   # A is back although B was refused
            self.assertFalse(P.history()[0]["undone"])
            fake.fail_on = None
            P.undo(bid)
        self.assertEqual(fake.d[("HKCU", "software\\x", "b")][0], 1)
        self.assertTrue(P.history()[0]["undone"])

    def test_binary_values_round_trip(self):
        fake = FakeReg({("HKCU", "software\\x", "blob"): (b"\x03\x00\xff", winreg.REG_BINARY)})
        with mock.patch.object(P, "REG", fake):
            bid = P.apply_writes([("HKCU", "Software\\X", "Blob", winreg.REG_DWORD, 1)], "test")
            P.undo(bid)
        self.assertEqual(fake.d[("HKCU", "software\\x", "blob")], (b"\x03\x00\xff", winreg.REG_BINARY))

    def test_findings(self):
        items = [{"id": "a", "name": "A", "on": True, "rec": "off", "w": 2}, {"id": "b", "name": "B", "on": False, "rec": "off", "w": 2},
                 {"id": "loc", "name": "L", "on": True, "rec": None, "w": 0}]
        f, ok = P.tracking_findings(items)
        self.assertEqual(f[0]["fix"]["arg"], ["a"])
        self.assertEqual(ok, [])
        f, ok = P.tracking_findings([dict(items[0], on=False)])
        self.assertEqual(f, [])
        self.assertTrue(ok)


class Findings(unittest.TestCase):
    def test_protection_all_good(self):
        p = {"av": [{"name": "Kaspersky", "on": True, "uptodate": True, "defender": False}], "defender": {"active": False},
             "firewall": {"profiles": [{"name": "Public", "on": False}], "third": ["Kaspersky"]}, "uac": "default",
             "secureBoot": True, "tpm": {"present": True, "version": "2.0"}, "bitlocker": "on", "hvci": True,
             "smartscreen": {"apps": "on", "edge": "on"}, "patch": {"days": 7}}
        f, ok = P.protection_findings(p)
        self.assertEqual(f, [])  # the firewall profile is off but a third-party firewall is on
        self.assertGreaterEqual(len(ok), 7)

    def test_protection_problems(self):
        p = {"av": [{"name": "Windows Defender", "on": False, "uptodate": True, "defender": True}], "defender": {"active": False},
             "firewall": {"profiles": [{"name": "Public", "on": False}], "third": []}, "uac": "never", "secureBoot": False,
             "bitlocker": "off", "hvci": False, "smartscreen": {"apps": "off"}, "patch": {"days": 90}}
        f, _ = P.protection_findings(p)
        ids = {x["id"] for x in f}
        self.assertTrue({"av-off", "fw-off", "uac", "secureboot", "bitlocker", "hvci", "smartscreen", "patch"} <= ids)
        self.assertEqual(next(x for x in f if x["id"] == "fw-off")["fix"]["arg"], ["Public"])
        self.assertLess(P.score(f), 30)

    def test_perm_findings(self):
        now = time.time()
        caps = [{"id": "webcam", "name": "Camera", "page": "ms-settings:privacy-webcam",
                 "apps": [{"name": "Teams", "active": True, "start": now - 60}]},
                {"id": "microphone", "name": "Microphone", "page": "ms-settings:privacy-microphone",
                 "apps": [{"name": "A", "active": False, "start": now - 3600}, {"name": "B", "active": False, "start": now - 30 * 86400}]}]
        f, ok = P.perm_findings(caps, now)
        self.assertEqual([x["id"] for x in f], ["inuse-webcam", "recent-microphone"])
        self.assertEqual(ok, [])
        self.assertTrue(all(x["w"] == 0 for x in f))

    def test_name_list(self):
        self.assertEqual(P.name_list(["a", "b"]), "a, b")
        self.assertIn("2", P.name_list(["a", "b", "c", "d", "e"]))


if __name__ == "__main__":
    unittest.main()

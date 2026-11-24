"""Backups and restore engine: parsers of Windows output, shadow copy pairing, failure reasons, the 24-hour limit
override (with a fake registry: nothing here touches the real one) and the Super Optimizer backup listing."""
import json
import os
import shutil
import tempfile
import unittest
import winreg
from unittest import mock

from rshub.engine import backups as B

GUID_C = "56580811-b55b-450b-bb57-22893f4cf5b3"
GUID_D = "91657175-2175-4660-bd85-9f82825c23ac"

VSS_EN = rf"""vssadmin 1.1 - Volume Shadow Copy Service administrative command-line tool
(C) Copyright 2001-2013 Microsoft Corp.

Shadow Copy Storage association
   For volume: (C:)\\?\Volume{{{GUID_C}}}\
   Shadow Copy Storage volume: (C:)\\?\Volume{{{GUID_C}}}\
   Used Shadow Copy Storage space: 3.21 GB (0%)
   Allocated Shadow Copy Storage space: 3.73 GB (0%)
   Maximum Shadow Copy Storage space: 47.6 GB (5%)

Shadow Copy Storage association
   For volume: (D:)\\?\Volume{{{GUID_D}}}\
   Shadow Copy Storage volume: (D:)\\?\Volume{{{GUID_D}}}\
   Used Shadow Copy Storage space: 0 bytes (0%)
   Allocated Shadow Copy Storage space: 0 bytes (0%)
   Maximum Shadow Copy Storage space: UNBOUNDED (100%)
"""
VSS_ES = rf"""vssadmin 1.1 - Herramienta administrativa de línea de comandos del Servicio de instantáneas de volumen.
(C) Copyright 2001-2013 Microsoft Corp.

Asociación de almacenamiento de instantáneas
   Para volumen: (C:)\\?\Volume{{{GUID_C}}}\
   Volumen de almacenamiento de instantáneas: (C:)\\?\Volume{{{GUID_C}}}\
   Espacio usado de almacenamiento de instantáneas: 3,21 GB (0%)
   Espacio asignado de almacenamiento de instantáneas: 512 MB (0%)
   Espacio máximo de almacenamiento de instantáneas: 47,6 GB (5%)
"""
VSS_NONE = """vssadmin 1.1 - Volume Shadow Copy Service administrative command-line tool
(C) Copyright 2001-2013 Microsoft Corp.

No items found that satisfy the query.
"""


class Parsers(unittest.TestCase):
    def test_wmi_time(self):
        utc = B.parse_wmi_time("20261118163012.000000-000")
        self.assertEqual(utc, B.parse_wmi_time("2026-11-18T16:30:12Z"))
        # a local time 5 hours behind UTC (offset -300 minutes) is the same instant as 21:30 UTC
        self.assertEqual(B.parse_wmi_time("20261118163012.000000-300"), B.parse_wmi_time("2026-11-18T21:30:12+00:00"))
        self.assertAlmostEqual(B.parse_wmi_time("20261118163012.5-000") - utc, 0.5)
        self.assertEqual(B.parse_wmi_time("/Date(1795015490000)/"), 1795015490.0)
        self.assertIsNone(B.parse_wmi_time("yesterday"))
        self.assertIsNone(B.parse_wmi_time(None))

    def test_restore_points(self):
        rows = [{"seq": 27, "desc": "Automatic Restore Point", "type": 7, "t": "20261028170600.000000-000"},
                {"SequenceNumber": "31", "Description": "RsHub: before installing drivers", "RestorePointType": 10,
                 "CreationTime": "20261118120600.000000-000"},
                {"seq": None, "desc": "broken"}, "junk"]
        pts = B.parse_restore_points(rows)
        self.assertEqual([p["seq"] for p in pts], [31, 27])
        self.assertEqual(pts[0]["type"], 10)
        self.assertEqual(pts[0]["desc"], "RsHub: before installing drivers")
        self.assertIsNotNone(pts[1]["t"])
        self.assertEqual(B.parse_restore_points(None), [])

    def test_spp_clients(self):
        v = [rf"\\?\Volume{{{GUID_C.upper()}}}\:(C%3A)", rf"\\?\Volume{{{GUID_D}}}\:(D%3A)", "garbage"]
        self.assertEqual(B.parse_spp_clients(v), {GUID_C: "C:", GUID_D: "D:"})
        self.assertEqual(B.parse_spp_clients([rf"\\?\Volume{{{GUID_C}}}\ "]), {GUID_C: None})
        self.assertEqual(B.parse_spp_clients(None), {})

    def test_sizes(self):
        G = 1024 ** 3
        self.assertEqual(B.parse_size("47.6 GB (5%)"), round(47.6 * G))
        self.assertEqual(B.parse_size("47,6 GB (5%)"), round(47.6 * G))
        self.assertEqual(B.parse_size("1.234,5 MB"), round(1234.5 * 1024 ** 2))
        self.assertEqual(B.parse_size("1,234.5 MB"), round(1234.5 * 1024 ** 2))
        self.assertEqual(B.parse_size("0 bytes (0%)"), 0)
        self.assertEqual(B.parse_size("512 KB"), 512 * 1024)
        self.assertEqual(B.parse_size("1,5 Go"), round(1.5 * G))
        self.assertIsNone(B.parse_size("UNBOUNDED (100%)"))
        self.assertIsNone(B.parse_size("ILIMITADO (100%)"))
        self.assertEqual(B.parse_pct("47.6 GB (5%)"), 5.0)
        self.assertIsNone(B.parse_pct("47.6 GB"))

    def test_shadowstorage_english(self):
        st = B.parse_shadowstorage(VSS_EN)
        self.assertEqual(len(st), 2)
        c, d = st
        self.assertEqual((c["drive"], c["vol"], c["on"]), ("C:", GUID_C, "C:"))
        self.assertEqual(c["used"], round(3.21 * 1024 ** 3))
        self.assertEqual(c["max"], round(47.6 * 1024 ** 3))
        self.assertEqual(c["maxPct"], 5.0)
        self.assertEqual(d["used"], 0)
        self.assertIsNone(d["max"])  # unbounded

    def test_shadowstorage_translated(self):
        """The labels change with the Windows language; the parser only relies on the order of the lines."""
        st = B.parse_shadowstorage(VSS_ES)
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["drive"], "C:")
        self.assertEqual(st[0]["alloc"], 512 * 1024 ** 2)
        self.assertEqual(st[0]["max"], round(47.6 * 1024 ** 3))
        self.assertEqual(B.parse_shadowstorage(VSS_NONE), [])
        self.assertEqual(B.parse_shadowstorage(""), [])

    def test_storage_from_cim(self):
        rows = [{"vol": rf"\\?\Volume{{{GUID_C}}}\ ", "used": 100, "alloc": 200, "max": 5000},
                {"vol": rf"\\?\Volume{{{GUID_D}}}\ ", "used": 0, "alloc": 0, "max": 2 ** 64 - 1}, {"vol": "nothing"}]
        st = B.storage_from_cim(rows)
        self.assertEqual([s["vol"] for s in st], [GUID_C, GUID_D])
        self.assertEqual(st[0]["max"], 5000)
        self.assertIsNone(st[1]["max"])


class Shadows(unittest.TestCase):
    T = B.parse_wmi_time("2026-11-18T12:00:00Z")

    def sh(self, sid, dt, guid=GUID_C):
        return {"id": "{" + sid + "}", "vol": rf"\\?\Volume{{{guid}}}\ ", "t": f"2026-11-18T12:00:{dt:02d}Z"}

    def test_pairs_the_only_close_shadow(self):
        pts = [{"seq": 31, "t": self.T}, {"seq": 30, "t": self.T - 86400}]
        sid = "11111111-2222-3333-4444-555555555555"
        out = B.match_shadows(pts, [self.sh(sid, 5)], GUID_C)
        self.assertEqual(out, {31: sid})

    def test_ambiguous_or_other_volume_stays_unpaired(self):
        pts = [{"seq": 31, "t": self.T}]
        a, b = "11111111-2222-3333-4444-555555555555", "66666666-2222-3333-4444-555555555555"
        self.assertEqual(B.match_shadows(pts, [self.sh(a, 5), self.sh(b, 9)], GUID_C), {})  # two candidates
        self.assertEqual(B.match_shadows(pts, [self.sh(a, 5, GUID_D)], GUID_C), {})  # another drive
        self.assertEqual(B.match_shadows([{"seq": 31, "t": self.T}, {"seq": 32, "t": self.T + 30}], [self.sh(a, 10)], GUID_C), {})  # two points
        self.assertEqual(B.match_shadows(pts, [{"id": "not-a-guid", "vol": GUID_C, "t": "2026-11-18T12:00:01Z"}], GUID_C), {})
        far = {"id": a, "vol": rf"\\?\Volume{{{GUID_C}}}\ ", "t": "2026-11-18T12:10:00Z"}
        self.assertEqual(B.match_shadows(pts, [far], GUID_C), {})


class Reasons(unittest.TestCase):
    def test_classify(self):
        e = {"err": "The service cannot be started (Exception from HRESULT: 0x80070422)", "hr": "80070422"}
        self.assertEqual(B.classify_failure(e)[0], "protection_off")
        self.assertEqual(B.classify_failure(e, services_off=True)[0], "service_disabled")
        self.assertEqual(B.classify_failure({"err": "Access is denied", "hr": "80070005"})[0], "needs_admin")
        r, d = B.classify_failure({"warn": ["A new system restore point cannot be created because one has already been created within the past 1440 minutes."]})
        self.assertEqual(r, "failed")
        self.assertIn("1440", d)
        self.assertEqual(B.classify_failure({}), ("failed", ""))

    def test_messages_exist_for_every_reason(self):
        for r in ("created", "needs_admin", "protection_off", "service_disabled", "policy", "failed"):
            self.assertTrue(B.reason_text(r, drive="C:"))
        self.assertIn("boom", B.reason_text("failed", "boom"))


class FakeReg:
    def __init__(self, value=None, typ=winreg.REG_DWORD, fail_write=False, fail_restore=False):
        self.exists, self.value, self.type = value is not None, value, typ
        self.fail_write, self.fail_restore, self.writes = fail_write, fail_restore, []

    def read(self):
        return (self.exists, self.value, self.type) if self.exists else (False, None, None)

    def write(self, value, typ=winreg.REG_DWORD):
        if self.fail_write or (self.fail_restore and self.writes):
            raise PermissionError("denied")
        self.writes.append(value)
        self.exists, self.value, self.type = True, value, typ

    def delete(self):
        if self.fail_restore:
            raise PermissionError("denied")
        self.exists, self.value, self.type = False, None, None


class FakeMarker:
    def __init__(self, d=None):
        self.d = d

    def load(self):
        return self.d

    def save(self, d):
        self.d = dict(d)

    def clear(self):
        self.d = None


class FrequencyOverride(unittest.TestCase):
    def test_missing_value_is_removed_again(self):
        reg, mk = FakeReg(), FakeMarker()
        with B.frequency_override(reg, mk) as lifted:
            self.assertTrue(lifted)
            self.assertEqual(reg.read(), (True, 0, winreg.REG_DWORD))
            self.assertEqual(mk.d, {"exists": False, "value": None, "type": None})  # saved before the change
        self.assertEqual(reg.read(), (False, None, None))
        self.assertIsNone(mk.d)

    def test_previous_value_and_type_come_back(self):
        reg, mk = FakeReg(1440), FakeMarker()
        with B.frequency_override(reg, mk):
            self.assertEqual(reg.value, 0)
        self.assertEqual(reg.read(), (True, 1440, winreg.REG_DWORD))
        reg = FakeReg("720", winreg.REG_SZ)
        with B.frequency_override(reg, FakeMarker()):
            pass
        self.assertEqual(reg.read(), (True, "720", winreg.REG_SZ))

    def test_restored_even_if_the_creation_fails(self):
        reg, mk = FakeReg(60), FakeMarker()
        with self.assertRaises(RuntimeError):
            with B.frequency_override(reg, mk):
                raise RuntimeError("PowerShell crashed")
        self.assertEqual(reg.value, 60)
        self.assertIsNone(mk.d)

    def test_cannot_write_changes_nothing(self):
        reg, mk = FakeReg(1440, fail_write=True), FakeMarker()
        with B.frequency_override(reg, mk) as lifted:
            self.assertFalse(lifted)
        self.assertEqual(reg.read(), (True, 1440, winreg.REG_DWORD))
        self.assertIsNone(mk.d)

    def test_failed_restore_keeps_the_marker_and_recovery_finishes_it(self):
        reg, mk = FakeReg(1440, fail_restore=True), FakeMarker()
        with B.frequency_override(reg, mk):
            pass
        self.assertEqual(reg.value, 0)  # Windows refused to put it back...
        self.assertEqual(mk.d["value"], 1440)  # ...so the original stays written down
        reg.fail_restore = False
        self.assertTrue(B.recover_override(reg, mk))
        self.assertEqual(reg.read(), (True, 1440, winreg.REG_DWORD))
        self.assertIsNone(mk.d)
        self.assertFalse(B.recover_override(reg, mk))  # nothing left to do

    def test_interrupted_run_is_repaired_before_a_new_override(self):
        """A crash left 0 in the registry; the next override must restore and capture the ORIGINAL value, not 0."""
        reg, mk = FakeReg(0), FakeMarker({"exists": False, "value": None, "type": None})
        with B.frequency_override(reg, mk):
            self.assertEqual(reg.value, 0)
            self.assertEqual(mk.d, {"exists": False, "value": None, "type": None})
        self.assertEqual(reg.read(), (False, None, None))

    def test_failed_recovery_does_not_overwrite_the_original_with_zero(self):
        """Recovery could not write the original back (the 0 of a crashed run is still there): the marker is the only
        copy of the user's value, and the new override must put THAT back, not the 0 it reads."""
        class FirstWriteFails(FakeReg):
            def write(self, value, typ=winreg.REG_DWORD):
                if not getattr(self, "failed_once", False):
                    self.failed_once = True
                    raise PermissionError("busy")
                super().write(value, typ)
        reg, mk = FirstWriteFails(0), FakeMarker({"exists": True, "value": 1440, "type": winreg.REG_DWORD})
        with B.frequency_override(reg, mk) as lifted:
            self.assertTrue(lifted)
            self.assertEqual(reg.value, 0)
        self.assertEqual(reg.read(), (True, 1440, winreg.REG_DWORD))
        self.assertIsNone(mk.d)

    def test_real_marker_file_roundtrip(self):
        d = tempfile.mkdtemp()
        try:
            mk = B._Marker(os.path.join(d, "sub", "m.json"))
            self.assertIsNone(mk.load())
            mk.save({"exists": True, "value": 5, "type": 4})
            self.assertEqual(mk.load(), {"exists": True, "value": 5, "type": 4})
            mk.clear()
            mk.clear()
            self.assertIsNone(mk.load())
        finally:
            shutil.rmtree(d)


class CreateRestorePoint(unittest.TestCase):
    """The helper with Windows replaced: no PowerShell, no registry."""

    def setUp(self):
        self.reg = FakeReg(1440)
        patches = [mock.patch.object(B, "_Reg", lambda: self.reg), mock.patch.object(B, "_Marker", FakeMarker),
                   mock.patch.object(B, "_precheck", lambda: None), mock.patch.object(B, "_services_off", lambda: [])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, out):
        seen = {}

        def fake_ps(script, timeout=0):
            seen["limit"] = self.reg.value  # what the registry held while Windows was creating it
            seen["script"] = script
            return out
        with mock.patch.object(B, "_ps_obj", fake_ps):
            r = B.create_restore_point("Before 'it' — test", kind="DEVICE_DRIVER_INSTALL")
        return r, seen

    def test_created_and_verified(self):
        r, seen = self.run_with({"before": 30, "after": 31, "desc": "Before 'it' — test"})
        self.assertTrue(r["ok"])
        self.assertEqual((r["reason"], r["seq"]), ("created", 31))
        self.assertEqual(seen["limit"], 0)
        self.assertEqual(self.reg.value, 1440)
        self.assertIn("DEVICE_DRIVER_INSTALL", seen["script"])
        self.assertNotIn("Before 'it'", seen["script"])  # the name travels base64-encoded, never inside quotes

    def test_first_point_ever(self):
        r, _ = self.run_with({"before": None, "after": 1})
        self.assertTrue(r["ok"])

    def test_no_new_point_is_a_failure(self):
        r, _ = self.run_with({"before": 31, "after": 31, "warn": ["created within the past 1440 minutes"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "failed")
        self.assertIn("1440", r["detail"])
        self.assertEqual(self.reg.value, 1440)

    def test_protection_off_from_windows_error(self):
        r, _ = self.run_with({"before": 31, "after": 31, "err": "The service cannot be started", "hr": "80070422"})
        self.assertEqual(r["reason"], "protection_off")
        self.assertTrue(r["msg"])

    def test_no_answer(self):
        r, _ = self.run_with(None)
        self.assertEqual(r["reason"], "failed")

    def test_precheck_stops_before_touching_anything(self):
        for reason in ("needs_admin", "protection_off", "service_disabled", "policy"):
            reg = FakeReg(1440)
            with mock.patch.object(B, "_precheck", lambda: (reason, "")), mock.patch.object(B, "_Reg", lambda: reg), \
                    mock.patch.object(B, "_ps_obj", side_effect=AssertionError("must not run")):
                r = B.create_restore_point("x")
            self.assertEqual((r["ok"], r["reason"]), (False, reason))
            self.assertEqual(reg.writes, [])


class OptimizerBackups(unittest.TestCase):
    def test_listing(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "registry", "20261118-161722"))
            with open(os.path.join(d, "registry", "20261118-161722", "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump([{"file": "000.reg", "view": 0, "k": r"HKCU\Software\Classes\.apk"}], fh)
            with open(os.path.join(d, "registry", "20261118-161722", "000.reg"), "w") as fh:
                fh.write("x" * 100)
            os.makedirs(os.path.join(d, "services"))
            with open(os.path.join(d, "services", "20261118-004716.json"), "w", encoding="utf-8") as fh:
                json.dump({"t": 1794959234.5, "prev": {"Spooler": "auto", "DiagTrack": "auto"}}, fh)
            with open(os.path.join(d, "services", "broken.json"), "w") as fh:
                fh.write("{")
            os.makedirs(os.path.join(d, "debloat", "20261117-1000"))  # Debloat's undo records: not a backup to delete
            os.makedirs(os.path.join(d, "network", "20261117-1100-ab12"))
            out = {b["id"]: b for b in B.optimizer_backups(d)}
            reg = out["opt-registry|20261118-161722"]
            self.assertEqual((reg["count"], reg["sample"]), (1, [r"HKCU\Software\Classes\.apk"]))
            self.assertGreaterEqual(reg["size"], 100)
            svc = out["opt-services|20261118-004716"]
            self.assertEqual((svc["count"], svc["sample"], svc["ts"]), (2, ["DiagTrack", "Spooler"], 1794959234.5))
            self.assertEqual(out["opt-services|broken"]["count"], 0)
            self.assertNotIn("opt-other|debloat|20261117-1000", out)
            self.assertIn("opt-other|network|20261117-1100-ab12", out)
            self.assertEqual(B.optimizer_backups(os.path.join(d, "missing")), [])
        finally:
            shutil.rmtree(d)

    def test_delete_own_only_accepts_listed_ids(self):
        with mock.patch.object(B, "optimizer_backups", lambda: [{"id": "opt-services|1", "kind": "opt-services", "path": "C:\\x\\1.json"}]), \
                mock.patch.object(B, "driver_backups", lambda: []), mock.patch.object(B, "_recycle", return_value=True) as rec:
            job = mock.Mock(cancelled=False)
            r = B.delete_own(job, ["opt-services|1", "quarantine|abc", "opt-registry|..\\..\\Windows"])
        self.assertEqual(r["deleted"], ["opt-services|1"])
        self.assertEqual(r["failed"], ["quarantine|abc", "opt-registry|..\\..\\Windows"])
        rec.assert_called_once_with("C:\\x\\1.json")


if __name__ == "__main__":
    unittest.main()

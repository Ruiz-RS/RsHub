"""Safety rules: they only answer whether something can be deleted, they never delete."""
import os
import unittest

from rshub.engine.removal import paths, safety
from rshub.engine.removal.registry import V32, V64, RegRef

K = paths.known_folders()
PF, LOCAL, ROAM = K["ProgramFiles"], K["LocalAppData"], K["RoamingAppData"]
CF = K["ProgramFilesCommon"]


def ctx(target=None, programs=(), shared=None):
    return safety.Context(target=target, programs=programs, shared_dlls=shared or {})


class PathRules(unittest.TestCase):
    def blocked(self, p, c=None):
        return (c or ctx()).check_path(p)[0]

    def test_system_locations_blocked(self):
        for p in [K["Windows"], os.path.join(K["Windows"], "System32", "drivers", "x.sys"), "C:\\", "D:\\",
                  PF, K["ProgramFilesX86"], CF, K["ProgramData"], K["Profile"], LOCAL, ROAM, K["Documents"],
                  K["Desktop"], os.path.join(K["ProgramData"], "Microsoft", "Windows Defender"),
                  os.path.join(PF, "WindowsApps", "x"), os.path.join(LOCAL, "Microsoft", "Edge"),
                  os.path.join(ROAM, "Microsoft", "Word"), os.path.join(CF, "microsoft shared", "ink"),
                  os.path.join(LOCAL, "Packages", "Microsoft.X"), os.path.join(CF, "VST3"),
                  os.path.join(K["ProgramData"], "Package Cache", "{x}"), paths.DATA_DIR, paths.APP_DIR,
                  os.path.join(K["Profile"], "..")]:
            self.assertIsNotNone(self.blocked(p), p)

    def test_ancestor_of_protected_is_blocked(self):
        self.assertIsNotNone(self.blocked(os.path.dirname(K["Profile"])))  # C:\Users

    def test_normal_program_locations_allowed(self):
        for p in [os.path.join(PF, "FooApp"), os.path.join(LOCAL, "FooApp"), os.path.join(ROAM, "FooApp", "x.ini"),
                  os.path.join(LOCAL, "Programs", "foo"), os.path.join(CF, "VST3", "Serum2.vst3"),
                  os.path.join(K["Programs"], "FooApp"), os.path.join(K["CommonPrograms"], "FooApp.lnk"),
                  os.path.join(LOCAL, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg_x"),
                  os.path.join(K["Desktop"], "FooApp.lnk")]:
            self.assertIsNone(self.blocked(p), p)

    def test_personal_data_flagged(self):
        b, personal = ctx().check_path(os.path.join(K["Documents"], "FabFilter"))
        self.assertIsNone(b)
        self.assertTrue(personal)
        self.assertFalse(ctx().check_path(os.path.join(LOCAL, "FooApp"))[1])

    def test_folder_containing_other_program_blocked(self):
        steam = {"id": "steam", "name": "Steam", "location": r"C:\Games\Steam"}
        wall = {"id": "wall", "name": "Wallpaper Engine", "location": r"C:\Games\Steam\steamapps\common\wallpaper_engine"}
        c = ctx(target=steam, programs=[steam, wall])
        self.assertIn("Wallpaper Engine", c.check_path(r"C:\Games\Steam")[0])
        self.assertIsNone(c.check_path(r"C:\Games\Steam\logs")[0])

    def test_nested_program_only_its_own_folder(self):
        steam = {"id": "steam", "name": "Steam", "location": r"C:\Games\Steam"}
        wall = {"id": "wall", "name": "Wallpaper Engine", "location": r"C:\Games\Steam\steamapps\common\wallpaper_engine"}
        c = ctx(target=wall, programs=[steam, wall])
        self.assertIsNone(c.check_path(r"C:\Games\Steam\steamapps\common\wallpaper_engine")[0])
        self.assertIn("Steam", c.check_path(r"C:\Games\Steam\config")[0])

    def test_shared_location_between_two_programs_blocked(self):
        a = {"id": "a", "name": "AURA x86", "location": r"C:\Program Files\AURA"}
        b = {"id": "b", "name": "AURA x64", "location": r"C:\Program Files\AURA"}
        self.assertIn("AURA x64", ctx(target=a, programs=[a, b]).check_path(r"C:\Program Files\AURA")[0])

    def test_shared_dlls(self):
        shared = {paths.norm_path(r"C:\Program Files\Foo\common.dll"): 2}
        self.assertIsNotNone(ctx(shared=shared).check_path(r"C:\Program Files\Foo"))
        shared1 = {paths.norm_path(r"C:\Program Files\Foo\common.dll"): 1}
        self.assertIsNone(ctx(shared=shared1).check_path(r"C:\Program Files\Foo")[0])


class RegRules(unittest.TestCase):
    def ok(self, hive, path, view=0, value=None):
        return safety.reg_rule(RegRef(hive, path, view, value)) is None

    def test_blocked(self):
        for hive, path, value in [
            ("HKLM", "SOFTWARE", None), ("HKLM", r"SOFTWARE\Microsoft", None), ("HKLM", r"SOFTWARE\Microsoft\Office", None),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", None), ("HKLM", r"SOFTWARE\Classes\.zip", None),
            ("HKLM", r"SOFTWARE\Classes\CLSID", None), ("HKLM", r"SOFTWARE\Classes\exefile", None),
            ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager", None), ("HKLM", r"SOFTWARE\Policies\Foo", None),
            ("HKCU", r"Software\Classes\*", None), ("HKLM", r"SOFTWARE\WOW6432Node", None),
            ("HKLM", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "x"), ("HKLM", r"SYSTEM\CurrentControlSet\Services\wuauserv", None),
            ("HKCU", r"Software\Classes\Windows.Photos", None),
        ]:
            self.assertFalse(self.ok(hive, path, value=value), f"{hive}\\{path} {value}")

    def test_allowed(self):
        for hive, path, view, value in [
            ("HKCU", r"Software\FooApp", 0, None), ("HKLM", r"SOFTWARE\FooApp", V32, None),
            ("HKLM", r"SOFTWARE\WOW6432Node\FooApp", 0, None), ("HKCU", r"Software\FooVendor\FooApp", 0, None),
            ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\FooApp", V64, None),
            ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\foo.exe", V64, None),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", 0, "FooApp"),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run", 0, "FooApp"),
            ("HKLM", r"SOFTWARE\Classes\7-Zip.zip", 0, None), ("HKCU", r"Software\Classes\.zip\OpenWithProgids", 0, "7-Zip.zip"),
            ("HKLM", r"SOFTWARE\Classes\CLSID\{23170F69-40C1-278A-1000-000100020000}", 0, None),
            ("HKLM", r"SOFTWARE\Classes\Installer\Products\4746367D9FA0BB343B054DC36449E9FC", 0, None),
            ("HKLM", r"SOFTWARE\Classes\Applications\foo.exe", 0, None),
            ("HKLM", r"SYSTEM\CurrentControlSet\Services\MiniRevoServicioQueNoExiste", 0, None),
            ("HKU", r"S-1-5-21-1\Software\FooApp", 0, None),
        ]:
            self.assertTrue(self.ok(hive, path, view, value), f"{hive}\\{path} {value}")

    def test_kernel_driver_and_boot_services_blocked(self):
        self.assertIn("driver", safety.service_rule("x", {"type": 1, "start": 3, "image": r"C:\foo\x.sys"}))
        self.assertIn("boot", safety.service_rule("x", {"type": 16, "start": 1, "image": r"C:\foo\x.exe"}))
        self.assertIn("Windows", safety.service_rule("x", {"type": 32, "start": 3,
                                                           "image": r"%SystemRoot%\System32\svchost.exe -k x"}))
        self.assertIsNone(safety.service_rule("x", {"type": 16, "start": 2, "image": r'"C:\Program Files\Foo\svc.exe"'}))

    def test_real_disk_driver_blocked(self):
        self.assertIsNotNone(safety.service_rule("disk"))


class ProgramClassification(unittest.TestCase):
    def cat(self, name, pub=""):
        r = safety.classify_program({"name": name, "publisher": pub})
        return r and r["category"]

    def test_categories(self):
        self.assertEqual(self.cat("Kaspersky"), "security")
        self.assertEqual(self.cat("Microsoft Visual C++ 2022 X64 Minimum Runtime - 14.40"), "runtime")
        self.assertEqual(self.cat("NVIDIA Controlador de gráficos 617.42", "NVIDIA Corporation"), "driver")
        self.assertEqual(self.cat("Proton VPN", "Proton AG"), "network")
        self.assertEqual(self.cat("Microsoft Edge", "Microsoft Corporation"), "windows")
        self.assertIsNone(self.cat("7-Zip 26.05 (x64)", "Igor Pavlov"))
        self.assertIsNone(self.cat("FabFilter Pro-Q 4", "FabFilter"))
        self.assertIsNone(self.cat("Notepad++ (64-bit x64)", "Notepad++ Team"))


if __name__ == "__main__":
    unittest.main()


class OtherDrives(unittest.TestCase):
    """Shared folders on every drive are as protected as on C: (an installer may record "D:\Program Files")."""

    def test_shared_folders_on_every_drive(self):
        from unittest import mock
        from rshub.engine.removal import safety as sf
        from rshub.engine.removal.paths import norm_path
        with mock.patch.object(sf, "_drives", lambda: ["C:\\", "D:\\", "E:\\"]):
            pe, ft = sf.protected_exact(), sf.forbidden_trees()
        for p in (r"D:\Program Files", r"E:\Games", r"E:\SteamLibrary", r"D:\Program Files (x86)"):
            self.assertIn(norm_path(p), pe, p)
        for p in (r"D:\$Recycle.Bin", r"E:\System Volume Information", r"D:\WindowsApps"):
            self.assertIn(norm_path(p), ft, p)

import unittest

from rshub.engine.removal import programs


class Programs(unittest.TestCase):
    def test_pack_guid(self):
        self.assertEqual(programs.pack_guid("{D7636474-0AF9-43BB-B350-D43C46949ECF}"),
                         "4746367D9FA0BB343B054DC36449E9FC")
        self.assertIsNone(programs.pack_guid("nope"))

    def test_kind_detection(self):
        k = lambda **kw: programs.detect_kind({"keyname": "", "uninstall": "", **kw})
        self.assertEqual(k(keyname="Git_is1", uninstall=r'"C:\Program Files\Git\unins001.exe"'), "inno")
        self.assertEqual(k(windows_installer=True, uninstall="MsiExec.exe /X{A}"), "msi")
        self.assertEqual(k(uninstall=r'"C:\Program Files (x86)\Steam\steam.exe" steam://uninstall/431960'), "steam")
        self.assertEqual(k(uninstall=r'"C:\ProgramData\Package Cache\{x}\VC_redist.x64.exe" /uninstall'), "burn")
        self.assertEqual(k(uninstall=r'"C:\b\Application\1\Installer\setup.exe" --uninstall --system-level'), "chromium")
        self.assertEqual(k(uninstall=r'"C:\Program Files\7-Zip\Uninstall.exe"'), "nsis")
        self.assertEqual(k(uninstall=r"C:\Users\x\AppData\Local\Microsoft\WinGet\Packages\a\x.exe", keyname="Gyan.FFmpeg_Microsoft.Winget.Source"), "winget")

    def test_uninstall_commands(self):
        p = {"kind": "msi", "msi_code": "{ABC}", "uninstall": "MsiExec.exe /I{ABC}"}
        self.assertEqual(programs.uninstall_command(p), ("msiexec.exe /x {ABC}", False))
        self.assertEqual(programs.uninstall_command(p, True), ("msiexec.exe /x {ABC} /qn /norestart", True))
        p = {"kind": "inno", "uninstall": '"C:\\x\\unins000.exe"'}
        self.assertEqual(programs.uninstall_command(p, True)[0], '"C:\\x\\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART')
        p = {"kind": "other", "uninstall": "u.exe", "quiet": "u.exe /quiet"}
        self.assertEqual(programs.uninstall_command(p, True), ("u.exe /quiet", True))
        p = {"kind": "other", "uninstall": "u.exe"}
        self.assertEqual(programs.uninstall_command(p, True), ("u.exe", False))
        self.assertEqual(programs.uninstall_command({"kind": "other", "uninstall": ""}), (None, False))

    def test_exe_from_command(self):
        self.assertEqual(programs.exe_from_command('"C:\\a b\\u.exe" /x'), "C:\\a b\\u.exe")
        self.assertEqual(programs.exe_from_command("C:\\WinRAR\\uninstall.exe"), "C:\\WinRAR\\uninstall.exe")

    def test_real_inventory_sane(self):
        progs = programs.get_win32_programs()
        self.assertGreater(len(progs), 0)
        ids = [p["id"] for p in progs]
        self.assertEqual(len(ids), len(set(ids)))
        for p in progs:
            self.assertTrue(p["name"])
            self.assertIn(p["kind"], programs.KIND_LABEL)


if __name__ == "__main__":
    unittest.main()

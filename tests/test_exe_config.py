"""RsHub.exe.config must ship next to the exe: without it, a portable copy extracted by Windows Explorer (every
file marked as downloaded from the internet) stops at start with "Failed to resolve Python.Runtime.Loader.Initialize"."""
import os
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ExeConfig(unittest.TestCase):
    def test_config_allows_loading_its_own_assemblies(self):
        tree = ET.parse(os.path.join(ROOT, "packaging", "RsHub.exe.config"))
        el = tree.getroot().find("./runtime/loadFromRemoteSources")
        self.assertIsNotNone(el)
        self.assertEqual(el.get("enabled"), "true")

    def test_build_copies_it_next_to_the_exe(self):
        with open(os.path.join(ROOT, "packaging", "build.ps1"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn('"build\\dist\\RsHub\\RsHub.exe.config"', script)


if __name__ == "__main__":
    unittest.main()

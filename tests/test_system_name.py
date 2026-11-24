"""The PC's name on Home: BIOS placeholders ("System Product Name") are replaced by the motherboard's name."""
import unittest
from unittest import mock

from rshub.engine import system


def build(**fields):
    r = {"maker": "", "model": "", "family": "", "chassis": [3], "pcType": 1, "os": "Microsoft Windows 11 Pro", "osVer": "25H2",
         "cpu": {"name": "CPU"}, "gpus": [], "ram": [], "disks": [], "nets": [], "audio": [], "monitors": []}
    r.update(fields)
    with mock.patch.object(system, "_query", lambda: r), mock.patch.object(system, "_save", lambda *a: None, create=True):
        return system._build()


class Names(unittest.TestCase):
    def test_placeholder_model_uses_the_motherboard(self):
        info = build(maker="ASUS", model="System Product Name", family="To be filled by O.E.M.",
                     board="ROG STRIX Z790-E GAMING WIFI", boardMaker="ASUSTeK COMPUTER INC.")
        self.assertEqual(info["model"], "ASUS ROG STRIX Z790-E GAMING WIFI")
        self.assertNotIn("System Product Name", info["code"])

    def test_placeholder_maker_uses_the_board_maker(self):
        info = build(maker="System manufacturer", model="System Product Name", board="Z97 GAMING 3 (MS-7918)",
                     boardMaker="MSI")
        self.assertEqual(info["model"], "MSI Z97 GAMING 3 (MS-7918)")

    def test_real_names_are_kept(self):
        info = build(maker="ASUSTeK COMPUTER INC.", model="ASUS ROG Strix G16 G614JV_G614JV", family="ASUS ROG Strix G16",
                     board="G614JV", boardMaker="ASUSTeK COMPUTER INC.")
        self.assertEqual(info["model"], "ASUS ROG Strix G16")

    def test_nothing_usable_gives_a_generic_name(self):
        info = build(maker="To be filled by O.E.M.", model="To be filled by O.E.M.", board="Default string", boardMaker="Default string")
        self.assertNotIn("O.E.M", info["model"])
        self.assertNotIn("Default string", info["model"])


if __name__ == "__main__":
    unittest.main()

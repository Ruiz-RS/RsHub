"""What kind of PC this is (system.classify), from the strings real machines report: handhelds first, then tablets,
all-in-ones and mini PCs; laptops and desktops keep their old answer."""
import unittest
from types import SimpleNamespace
from unittest import mock

from rshub.engine import fakepc, system


def kind(maker="", model="", family="", board="", sku="", chassis=(), cpu="", battery=False, inches=None, pcType=None,
         pcTypeEx=None, boardMaker=""):
    r = {"maker": maker, "model": model, "family": family, "board": board, "sku": sku, "chassis": list(chassis),
         "cpu": {"name": cpu}, "pcType": pcType, "pcTypeEx": pcTypeEx, "boardMaker": boardMaker or maker}
    return system.classify(r, battery, inches)[0]


ASUS, LENOVO, MSI = "ASUSTeK COMPUTER INC.", "LENOVO", "Micro-Star International Co., Ltd."
HANDHELDS = [  # (name, fields): what Win32_ComputerSystem / Win32_BaseBoard report on each one
    ("ROG Ally", dict(maker=ASUS, model="ROG Ally RC71L_RC71L", family="ROG Ally", board="RC71L", chassis=[30],
                      cpu="AMD Ryzen Z1 Extreme w/ Radeon 780M Graphics")),
    ("ROG Ally X", dict(maker=ASUS, model="ROG Ally X RC72LA_RC72LA", family="ROG Ally X", board="RC72LA", chassis=[30])),
    ("ROG Xbox Ally X", dict(maker=ASUS, model="ROG Xbox Ally X RC73XA_RC73XA", board="RC73XA", chassis=[30])),
    ("ROG Xbox Ally (board only)", dict(maker=ASUS, model="System Product Name", board="RC73YA", chassis=[3])),
    ("Legion Go", dict(maker=LENOVO, model="83E1", family="Legion Go 8APU1", board="LNVNB161216", chassis=[32],
                       sku="LENOVO_MT_83E1_BU_idea_FM_Legion Go 8APU1")),
    ("Legion Go S", dict(maker=LENOVO, model="83L3", family="Legion Go S 8APU1", chassis=[32], cpu="AMD Ryzen Z2 Go")),
    ("Legion Go 2", dict(maker=LENOVO, model="83N0", family="Legion Go 2 8AHP2", chassis=[32])),
    ("Legion Go (type only)", dict(maker=LENOVO, model="83Q3", family="", chassis=[10])),
    ("MSI Claw A1M", dict(maker=MSI, model="Claw A1M", family="Claw", board="MS-1T41", chassis=[10])),
    ("MSI Claw 8 AI+", dict(maker=MSI, model="Claw 8 AI+ A2VM", board="MS-1T52", chassis=[10])),
    ("MSI Claw A8", dict(maker=MSI, model="To be filled by O.E.M.", board="MS-1T8K", chassis=[3])),
    ("Steam Deck LCD", dict(maker="Valve", model="Jupiter", family="Aerith", board="Jupiter", chassis=[1],
                            cpu="AMD Custom APU 0405")),
    ("Steam Deck OLED", dict(maker="Valve", model="Galileo", family="Sephiroth", board="Galileo", chassis=[1])),
    ("ZOTAC Zone", dict(maker="ZOTAC", model="ZOTAC GAMING ZONE", board="G0A1W", chassis=[3])),
    ("AYANEO 2S", dict(maker="AYANEO", model="AYANEO 2S", chassis=[3])),
    ("AYANEO AIR Plus", dict(maker="AYANEO", model="AB05-AMD", chassis=[3])),
    ("AYA NEO 2021", dict(maker="AYADEVICE", model="AYA NEO 2021", chassis=[3])),
    ("GPD Win 4", dict(maker="GPD", model="G1618-04", chassis=[10])),
    ("GPD Win Mini", dict(maker="GPD", model="G1617-01", chassis=[10])),
    ("GPD Win 2", dict(maker="GPD", model="WIN2", chassis=[10])),
    ("OneXPlayer 2 Pro", dict(maker="ONE-NETBOOK TECHNOLOGY CO., LTD.", model="ONEXPLAYER 2 PRO ARP23P", chassis=[3])),
    ("OneXFly", dict(maker="ONE-NETBOOK", model="ONEXPLAYER F1", chassis=[3])),
    ("OneXPlayer (first)", dict(maker="ONE-NETBOOK TECHNOLOGY CO., LTD.", model="ONE XPLAYER", chassis=[3])),
    ("AOKZOE A1 Pro", dict(maker="AOKZOE", model="AOKZOE A1 Pro", chassis=[3])),
    ("Ayn Loki Max", dict(maker="ayn", model="Loki Max", chassis=[3])),
    ("Anbernic Win600", dict(maker="Default string", boardMaker="Anbernic", model="Win600", chassis=[3])),
    ("an unlisted handheld with a Ryzen Z2", dict(maker="Acme", model="X7", chassis=[3], cpu="AMD Ryzen Z2 A")),
    ("an unlisted handheld with a small screen", dict(maker="Acme", model="X7", chassis=[3], inches=8,
                                                       cpu="AMD Ryzen 7 8840U w/ Radeon 780M Graphics")),
    ("chassis code 11 (hand held)", dict(maker="Acme", model="X9", chassis=[11])),
]
NOT_HANDHELDS = [  # (name, fields, expected kind)
    ("ASUS TUF Gaming F16", dict(maker=ASUS, model="ASUS TUF Gaming F16 FX608JPR_FX608JPR", family="ASUS TUF Gaming F16",
                                 board="FX608JPR", chassis=[10], battery=True, inches=16), "laptop"),
    ("ASUS ROG Strix G16", dict(maker=ASUS, model="ROG Strix G614JV_G614JV", family="ROG Strix", board="G614JV",
                                chassis=[10], battery=True, inches=16), "laptop"),
    ("Lenovo Legion 5", dict(maker=LENOVO, model="82JW", family="Legion 5 15ACH6H", chassis=[10], battery=True, inches=16), "laptop"),
    ("MSI Katana 15", dict(maker=MSI, model="Katana 15 B13VFK", board="MS-1585", chassis=[10], battery=True, inches=16), "laptop"),
    ("GPD Pocket 3 (a small laptop)", dict(maker="GPD", model="G1621-02", chassis=[10], battery=True, inches=8), "laptop"),
    ("an 8\" Atom tablet", dict(maker="Dell Inc.", model="Venue 8 Pro 5830", chassis=[30], battery=True, inches=8,
                                cpu="Intel(R) Atom(TM) CPU Z3740D @ 1.33GHz"), "tablet"),
    ("Surface Pro 9", dict(maker="Microsoft Corporation", model="Surface Pro 9", family="Surface", sku="Surface_Pro_9_2038",
                           chassis=[9], battery=True, inches=13), "tablet"),
    ("Surface Go 3", dict(maker="Microsoft Corporation", model="Surface Go 3", family="Surface", chassis=[9], battery=True,
                          inches=11), "tablet"),
    ("Surface Laptop 5", dict(maker="Microsoft Corporation", model="Surface Laptop 5", family="Surface", chassis=[9],
                              battery=True, inches=14), "laptop"),
    ("Surface Studio 2", dict(maker="Microsoft Corporation", model="Surface Studio 2", family="Surface", chassis=[13]), "aio"),
    ("ROG Flow Z13", dict(maker=ASUS, model="ROG Flow Z13 GZ301ZE_GZ301ZE", board="GZ301ZE", chassis=[32], battery=True,
                          inches=13), "tablet"),
    ("ThinkPad X1 Tablet", dict(maker=LENOVO, model="20KJ", family="ThinkPad X1 Tablet 3rd Gen", chassis=[32], battery=True,
                                inches=13), "tablet"),
    ("a detachable by its chassis code", dict(maker="Acme", model="D13", chassis=[32], battery=True, inches=13), "tablet"),
    ("a Slate by PCSystemTypeEx", dict(maker="Acme", model="S12", chassis=[2], pcTypeEx=8, battery=True, inches=12), "tablet"),
    ("HP All-in-One", dict(maker="HP", model="HP All-in-One 24-cb1xxx", chassis=[13]), "aio"),
    ("ThinkCentre M90a", dict(maker=LENOVO, model="11CD", family="ThinkCentre M90a", chassis=[3]), "aio"),
    ("iMac (Boot Camp)", dict(maker="Apple Inc.", model="iMac19,1", chassis=[13]), "aio"),
    ("Intel NUC", dict(maker="Intel(R) Client Systems", model="NUC11PAHi7", board="NUC11PABi7", chassis=[3]), "mini"),
    ("ASUS NUC 14 Pro", dict(maker=ASUS, model="NUC14RVH", board="NUC14RVB", chassis=[35]), "mini"),
    ("ThinkCentre M70q", dict(maker=LENOVO, model="11JN", family="ThinkCentre M70q Gen 3", chassis=[35]), "mini"),
    ("OptiPlex Micro", dict(maker="Dell Inc.", model="OptiPlex 7090 Micro", chassis=[3]), "mini"),
    ("Beelink SER", dict(maker="AZW", model="SER", chassis=[3]), "mini"),
    ("Mac mini (Boot Camp)", dict(maker="Apple Inc.", model="Macmini8,1", chassis=[3]), "mini"),
    ("AYANEO AM02 mini PC", dict(maker="AYANEO", model="AM02", chassis=[35]), "mini"),
    ("Valve Steam Machine", dict(maker="Valve", model="Fremont", chassis=[3]), "mini"),
    ("a mini-PC code on a laptop", dict(maker="Acme", model="L14", chassis=[35], battery=True, inches=14), "laptop"),
    ("a custom tower", dict(maker="System manufacturer", model="System Product Name", board="ROG STRIX Z790-E GAMING WIFI",
                            boardMaker=ASUS, chassis=[3]), "desktop"),
    ("VMware", dict(maker="VMware, Inc.", model="VMware Virtual Platform", chassis=[1]), "virtual"),
    ("no chassis code, mobile", dict(maker="Acme", model="M1", pcType=2, battery=True), "laptop"),
    ("no chassis code, desktop", dict(maker="Acme", model="D1", pcType=1), "desktop"),
]


class Classify(unittest.TestCase):
    def test_handhelds(self):
        for name, f in HANDHELDS:
            f = {"battery": True, "inches": 7, **f}
            with self.subTest(name):
                self.assertEqual(kind(**f), "handheld")

    def test_everything_else(self):
        for name, f, want in NOT_HANDHELDS:
            with self.subTest(name):
                self.assertEqual(kind(**f), want)

    def test_type_names(self):
        r = {"maker": ASUS, "model": "ROG Ally RC71L_RC71L", "board": "RC71L", "chassis": [30]}
        self.assertEqual(system.classify(r, True, 7), ("handheld", "Handheld"))
        self.assertEqual(system.classify({"maker": "Acme", "chassis": [32]}, True, 13), ("tablet", "Detachable"))
        self.assertEqual(system.classify({"maker": "Acme", "chassis": [13]}), ("aio", "All-in-one"))
        self.assertEqual(system.classify({"maker": "Acme", "chassis": [36]}), ("mini", "Stick PC"))
        self.assertEqual(system.classify({"maker": "Acme", "chassis": [7]}), ("desktop", "Tower"))

    def test_fake_pcs_are_recognised(self):
        """Each RSHUB_FAKE_PC poses with the strings its kind of PC reports, and classify agrees with it."""
        for k, raw in fakepc.RAW.items():
            with self.subTest(k):
                got = system.classify(raw, fakepc.BATTERY.get(k, False), fakepc.SCREEN.get(k))[0]
                self.assertEqual(got, k)


class BuildUsesIt(unittest.TestCase):
    """_build() feeds classify the battery and the built-in screen's size it measured."""
    def build(self, battery, **fields):
        r = {"maker": "Acme", "model": "X7", "family": "", "chassis": [3], "pcType": 1, "os": "Microsoft Windows 11 Home",
             "osVer": "24H2", "cpu": {"name": "AMD Ryzen 7 8840U"}, "gpus": [], "ram": [], "disks": [], "nets": [], "audio": [],
             "monitors": [{"inst": "M0", "name": "", "maker": "BOE", "code": "X"}],
             "monSizes": [{"inst": "M0", "w": 15, "h": 9}], "monConn": [{"inst": "M0", "tech": 11}]}
        r.update(fields)
        bat = SimpleNamespace(percent=80, power_plugged=False) if battery else None
        with mock.patch.object(system, "_query", lambda: r), mock.patch.object(system.psutil, "sensors_battery", lambda: bat):
            return system._build()

    def test_small_built_in_screen_and_battery(self):
        info = self.build(True)
        self.assertEqual((info["type"], info["typeName"]), ("handheld", "Handheld"))

    def test_same_box_without_battery_stays_a_desktop(self):
        self.assertEqual(self.build(False)["type"], "desktop")


if __name__ == "__main__":
    unittest.main()

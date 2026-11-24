"""Developer-only: a TEST copy that pretends to be another kind of PC (to review how Home looks on one).

RSHUB_FAKE_PC=desktop | handheld | tablet | aio | mini is honoured ONLY together with RSHUB_ISOLATED=1; in a
normal or installed run `kind()` is always None, `active()` False and nothing here is ever used. Wired at the narrowest
points: system.pc_info / pc_info_fresh / counts / health and the telemetry sample.

Each fake PC also keeps the raw strings its firmware would report (RAW), so the tests can check that system.classify
recognises it as the same kind."""
import os
import time

GB = 1024 ** 3
KINDS = ("desktop", "handheld", "tablet", "aio", "mini")


def kind():
    """The kind of PC this test copy poses as, or None (always None unless RSHUB_ISOLATED=1)."""
    if os.environ.get("RSHUB_ISOLATED") != "1":
        return None
    k = os.environ.get("RSHUB_FAKE_PC", "").strip().lower()
    return k if k in KINDS else None


def active():
    return kind() is not None


def _T(text, ctx=None, **kw):
    d = {"tr": text}
    if ctx:
        d["ctx"] = ctx
    if kw:
        d["kw"] = kw
    return d


def _ghz(v):
    return _T("{v} GHz", v={"num": v, "d": 1})


def _slots(n, slots):
    return _T("{n} of {slots} slots, free slots available" if slots > n else "{n} of {slots} slots", n=n, slots=slots)


def _battery(pct, plugged=False):
    return {"id": "battery", "name": "Battery", "title": _T("{pct}%, charging", pct=pct) if plugged else f"{pct}%",
            "rows": [["Status", _T("Plugged in" if plugged else "On battery", ctx="battery")]]}


# What each fake PC's firmware would report (Win32_ComputerSystem / Win32_SystemEnclosure / Win32_BaseBoard / CPU).
RAW = {
    "desktop": {"maker": "Micro-Star International Co., Ltd.", "model": "MS-7D75", "family": "To be filled by O.E.M.",
                "board": "MAG B650 TOMAHAWK WIFI (MS-7D75)", "boardMaker": "Micro-Star International Co., Ltd.",
                "chassis": [3], "pcType": 1, "cpu": {"name": "AMD Ryzen 7 7800X3D 8-Core Processor"}},
    "handheld": {"maker": "ASUSTeK COMPUTER INC.", "model": "ROG Ally RC71L_RC71L", "family": "ROG Ally", "sku": "",
                 "board": "RC71L", "boardMaker": "ASUSTeK COMPUTER INC.", "chassis": [30], "pcType": 2, "pcTypeEx": 8,
                 "cpu": {"name": "AMD Ryzen Z1 Extreme w/ Radeon 780M Graphics"}},
    "tablet": {"maker": "Microsoft Corporation", "model": "Surface Pro 9", "family": "Surface",
               "sku": "Surface_Pro_9_2038", "board": "Surface Pro 9", "boardMaker": "Microsoft Corporation", "chassis": [9],
               "pcType": 2, "pcTypeEx": 8, "cpu": {"name": "12th Gen Intel(R) Core(TM) i7-1255U"}},
    "aio": {"maker": "HP", "model": "HP EliteOne 800 G6 24 All-in-One PC", "family": "103C_53307F HP EliteOne",
            "board": "8723", "boardMaker": "HP", "chassis": [13], "pcType": 1, "cpu": {"name": "Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz"}},
    "mini": {"maker": "ASUSTeK COMPUTER INC.", "model": "NUC14RVH", "family": "RV", "board": "NUC14RVB",
             "boardMaker": "ASUSTeK COMPUTER INC.", "chassis": [35], "pcType": 1, "cpu": {"name": "Intel(R) Core(TM) Ultra 7 155H"}},
}
BATTERY = {"handheld": True, "tablet": True}
SCREEN = {"handheld": 7, "tablet": 13}   # the built-in screen, inches


def _desktop():
    """A custom-built ATX tower with a Ryzen 7 7800X3D and an RTX 4070 SUPER."""
    mons = [{"vendor": "Gigabyte", "name": "M27Q X", "code": "GBT2710", "inches": 27, "internal": False},
            {"vendor": "AOC", "name": "24G2", "code": "AOC2402", "inches": 24, "internal": False}]
    comps = [
        {"id": "cpu", "name": "Processor", "title": "AMD Ryzen 7 7800X3D 8-Core Processor",
         "rows": [["Cores", "8"], ["Threads", "16"], ["Base clock", _ghz(4.2)], ["L3 cache", "96 MB"]]},
        {"id": "gpu", "name": "Graphics", "title": "NVIDIA GeForce RTX 4070 SUPER",
         "rows": [["Memory", "12 GB"], ["Integrated", "AMD Radeon(TM) Graphics"], ["Driver", "32.0.15.6094"]], "vram": 12},
        {"id": "ram", "name": "Memory", "title": "32 GB DDR5",
         "rows": [["Speed", "6000 MT/s"], ["Manufacturer", "G.Skill"], ["Modules", _slots(2, 4)]], "slots": 4, "modules": 2},
        {"id": "ssd", "name": "Storage", "title": "Samsung SSD 980 PRO 2TB 2 TB",
         "rows": [["Type", "SSD NVMe"], ["Used on C:", "812 GB"], ["Free on C:", "1049 GB"], ["Disks", "2"]], "bar": 0.436},
        {"id": "display", "name": "Displays", "title": _T("{n} displays", n=2),
         "rows": [["External", "Gigabyte M27Q X 27\""], ["External", "AOC 24G2 24\""]], "monitors": mons},
        {"id": "wifi", "name": "Network", "title": "Realtek Gaming 2.5GbE Family Controller",
         "rows": [["Link", "2.5 Gbps"], ["Type", "Ethernet"]]},
        {"id": "audio", "name": "Audio", "title": "Realtek High Definition Audio", "rows": [["Others", "NVIDIA High Definition Audio, USB Audio"]]},
        {"id": "sys", "name": "System", "title": "Windows 11 Pro 24H2",
         "rows": [["Build", "26100.2605"], ["BIOS", "American Megatrends International, LLC. 3.20"], ["Motherboard", "MAG B650 TOMAHAWK WIFI"]]},
    ]
    return {"model": "MSI MAG B650 TOMAHAWK WIFI", "code": "", "type": "desktop", "typeName": "Tower", "maker": "Micro-Star International Co., Ltd.",
            "os": "Windows 11 Pro 24H2", "build": "26100", "bios": "American Megatrends International, LLC. 3.20", "components": comps,
            "facts": {"drivers": 118, "programs": 74, "freeC": 1049}}


def _handheld():
    """A 7" gaming handheld: Ryzen Z1 Extreme with Radeon 780M graphics, 16 GB LPDDR5, 512 GB NVMe, 1080p 120 Hz screen."""
    mons = [{"vendor": "BOE", "name": "", "code": "TL070FVXS01", "inches": 7, "internal": True}]
    comps = [
        {"id": "cpu", "name": "Processor", "title": "AMD Ryzen Z1 Extreme w/ Radeon 780M Graphics",
         "rows": [["Cores", "8"], ["Threads", "16"], ["Base clock", _ghz(3.3)], ["L3 cache", "16 MB"]]},
        {"id": "gpu", "name": "Graphics", "title": "AMD Radeon 780M Graphics",
         "rows": [["Memory", "4 GB"], ["Driver", "32.0.12033.1030"]], "vram": 4},
        {"id": "ram", "name": "Memory", "title": "16 GB LPDDR5",
         "rows": [["Speed", "6400 MT/s"], ["Manufacturer", "Samsung"], ["Modules", _slots(4, 4)]], "slots": 4, "modules": 4},
        {"id": "ssd", "name": "Storage", "title": "Micron 2400 MTFDKBK512QFM 512 GB",
         "rows": [["Type", "SSD NVMe"], ["Used on C:", "182 GB"], ["Free on C:", "294 GB"], ["Disks", "1"]], "bar": 0.382},
        {"id": "display", "name": "Displays", "title": _T("{n} display", n=1),
         "rows": [["Internal", "BOE TL070FVXS01 7\""]], "monitors": mons},
        _battery(76),
        {"id": "wifi", "name": "Network", "title": "MEDIATEK Wi-Fi 6E MT7922 (RZ616) 160MHz Wireless LAN Card",
         "rows": [["Link", "1.2 Gbps"], ["Type", "Native 802.11"]]},
        {"id": "audio", "name": "Audio", "title": "Realtek(R) Audio", "rows": [["Others", "AMD High Definition Audio Device"]]},
        {"id": "sys", "name": "System", "title": "Windows 11 Home 24H2",
         "rows": [["Build", "26100.2605"], ["BIOS", "RC71L.341"], ["Motherboard", "RC71L"]]},
    ]
    return {"model": "ASUS ROG Ally", "code": "RC71L", "type": "handheld", "typeName": "Handheld", "maker": "ASUSTeK COMPUTER INC.",
            "os": "Windows 11 Home 24H2", "build": "26100", "bios": "RC71L.341", "components": comps,
            "facts": {"drivers": 96, "programs": 41, "freeC": 294}}


def _tablet():
    """A 13" detachable tablet with a Core i7-1255U, 16 GB LPDDR5 and Wi-Fi 6E."""
    mons = [{"vendor": "LG Display", "name": "", "code": "LGD066A", "inches": 13, "internal": True}]
    comps = [
        {"id": "cpu", "name": "Processor", "title": "12th Gen Intel Core i7-1255U",
         "rows": [["Cores", "10"], ["Threads", "12"], ["Base clock", _ghz(1.7)], ["L3 cache", "12 MB"]]},
        {"id": "gpu", "name": "Graphics", "title": "Intel Iris Xe Graphics", "rows": [["Driver", "31.0.101.4502"]]},
        {"id": "ram", "name": "Memory", "title": "16 GB LPDDR5",
         "rows": [["Speed", "5200 MT/s"], ["Manufacturer", "Hynix"], ["Modules", _slots(4, 4)]], "slots": 4, "modules": 4},
        {"id": "ssd", "name": "Storage", "title": "KIOXIA KBG50ZNS512G 512 GB",
         "rows": [["Type", "SSD NVMe"], ["Used on C:", "211 GB"], ["Free on C:", "265 GB"], ["Disks", "1"]], "bar": 0.443},
        {"id": "display", "name": "Displays", "title": _T("{n} display", n=1),
         "rows": [["Internal", "LG Display LGD066A 13\""]], "monitors": mons},
        _battery(64, True),
        {"id": "wifi", "name": "Network", "title": "Intel(R) Wi-Fi 6E AX211 160MHz", "rows": [["Link", "1.2 Gbps"], ["Type", "Native 802.11"]]},
        {"id": "audio", "name": "Audio", "title": "Realtek(R) Audio", "rows": [["Others", "Intel Smart Sound Technology"]]},
        {"id": "sys", "name": "System", "title": "Windows 11 Pro 24H2",
         "rows": [["Build", "26100.2605"], ["BIOS", "Microsoft Corporation 18.101.140"], ["Motherboard", "Surface Pro 9"]]},
    ]
    return {"model": "Microsoft Surface Pro 9", "code": "", "type": "tablet", "typeName": "Tablet", "maker": "Microsoft Corporation",
            "os": "Windows 11 Pro 24H2", "build": "26100", "bios": "18.101.140", "components": comps,
            "facts": {"drivers": 104, "programs": 58, "freeC": 265}}


def _aio():
    """A 24" all-in-one with a Core i7-10700, 16 GB DDR4 and a 512 GB NVMe drive."""
    mons = [{"vendor": "AU Optronics", "name": "", "code": "AUO25ED", "inches": 24, "internal": True}]
    comps = [
        {"id": "cpu", "name": "Processor", "title": "Intel Core i7-10700 CPU @ 2.90GHz",
         "rows": [["Cores", "8"], ["Threads", "16"], ["Base clock", _ghz(2.9)], ["L3 cache", "16 MB"]]},
        {"id": "gpu", "name": "Graphics", "title": "Intel UHD Graphics 630", "rows": [["Driver", "31.0.101.2125"]]},
        {"id": "ram", "name": "Memory", "title": "16 GB DDR4",
         "rows": [["Speed", "2933 MT/s"], ["Manufacturer", "Samsung"], ["Modules", _slots(1, 2)]], "slots": 2, "modules": 1},
        {"id": "ssd", "name": "Storage", "title": "SAMSUNG MZVLB512HBJQ-000H1 512 GB",
         "rows": [["Type", "SSD NVMe"], ["Used on C:", "236 GB"], ["Free on C:", "240 GB"], ["Disks", "1"]], "bar": 0.496},
        {"id": "display", "name": "Displays", "title": _T("{n} display", n=1),
         "rows": [["Internal", "AU Optronics AUO25ED 24\""]], "monitors": mons},
        {"id": "wifi", "name": "Network", "title": "Intel(R) Wi-Fi 6 AX201 160MHz", "rows": [["Link", "866.7 Mbps"], ["Type", "Native 802.11"]]},
        {"id": "audio", "name": "Audio", "title": "Realtek High Definition Audio", "rows": [["Others", "Intel Display Audio"]]},
        {"id": "sys", "name": "System", "title": "Windows 11 Pro 24H2",
         "rows": [["Build", "26100.2605"], ["BIOS", "HP S21 Ver. 02.18.00"], ["Motherboard", "8723"]]},
    ]
    return {"model": "HP EliteOne 800 G6 24 All-in-One PC", "code": "", "type": "aio", "typeName": "All-in-one", "maker": "HP",
            "os": "Windows 11 Pro 24H2", "build": "26100", "bios": "S21 Ver. 02.18.00", "components": comps,
            "facts": {"drivers": 112, "programs": 63, "freeC": 240}}


def _mini():
    """A palm-sized mini PC with a Core Ultra 7 155H, 32 GB DDR5 SO-DIMMs and a 1 TB NVMe drive, on a 27" monitor."""
    mons = [{"vendor": "Dell", "name": "S2721DGF", "code": "DELA1E6", "inches": 27, "internal": False}]
    comps = [
        {"id": "cpu", "name": "Processor", "title": "Intel Core Ultra 7 155H",
         "rows": [["Cores", "16"], ["Threads", "22"], ["Base clock", _ghz(3.8)], ["L3 cache", "24 MB"]]},
        {"id": "gpu", "name": "Graphics", "title": "Intel Arc Graphics", "rows": [["Driver", "32.0.101.6078"]]},
        {"id": "ram", "name": "Memory", "title": "32 GB DDR5",
         "rows": [["Speed", "5600 MT/s"], ["Manufacturer", "Kingston"], ["Modules", _slots(2, 2)]], "slots": 2, "modules": 2},
        {"id": "ssd", "name": "Storage", "title": "WD_BLACK SN770 1TB 1 TB",
         "rows": [["Type", "SSD NVMe"], ["Used on C:", "305 GB"], ["Free on C:", "626 GB"], ["Disks", "1"]], "bar": 0.328},
        {"id": "display", "name": "Displays", "title": _T("{n} display", n=1),
         "rows": [["External", "Dell S2721DGF 27\""]], "monitors": mons},
        {"id": "wifi", "name": "Network", "title": "Intel(R) Ethernet Controller I226-V", "rows": [["Link", "2.5 Gbps"], ["Type", "802.3"]]},
        {"id": "audio", "name": "Audio", "title": "Realtek(R) Audio", "rows": [["Others", "Intel Display Audio"]]},
        {"id": "sys", "name": "System", "title": "Windows 11 Pro 24H2",
         "rows": [["Build", "26100.2605"], ["BIOS", "RVMTL357.0047"], ["Motherboard", "NUC14RVB"]]},
    ]
    return {"model": "ASUS NUC 14 Pro", "code": "NUC14RVH", "type": "mini", "typeName": "Mini PC", "maker": "ASUSTeK COMPUTER INC.",
            "os": "Windows 11 Pro 24H2", "build": "26100", "bios": "RVMTL357.0047", "components": comps,
            "facts": {"drivers": 101, "programs": 66, "freeC": 626}}


_BUILD = {"desktop": _desktop, "handheld": _handheld, "tablet": _tablet, "aio": _aio, "mini": _mini}


def pc_info():
    """Same shape as system._build()."""
    info = _BUILD[kind() or "desktop"]()
    info.update({"ts": time.time(), "v": 0, "fake": True})
    return info


def counts():
    f = _BUILD[kind() or "desktop"]()["facts"]
    return {"programs": f["programs"], "drivers": f["drivers"], "freeC": f["freeC"]}


def health():
    return {"defender": True, "sigAge": 0, "antivirus": ["Windows Defender"], "deviceErrors": 0, "errorNames": [],
            "restore": None, "restoreAccess": True}


_NO_BATTERY = {"battery": None, "charging": None, "batterySecs": None, "batteryPower": None, "batteryHealth": None,
               "batteryCycles": None, "batteryDesignWh": None, "batteryFullWh": None}
# the battery and hardware-size fields of a telemetry sample for each fake PC (the live loads stay this PC's own)
_TELE = {
    "desktop": {**_NO_BATTERY, "ramTotal": 32.0, "diskUsed": 812, "diskTotal": 1863, "igpu": None, "vramTotal": 12},
    "handheld": {"battery": 76, "charging": False, "batterySecs": 5280, "batteryPower": -14.6, "batteryHealth": 94,
                 "batteryCycles": 61, "batteryDesignWh": 40.0, "batteryFullWh": 37.6, "ramTotal": 15.3, "diskUsed": 182, "diskTotal": 476,
                 "igpu": None, "vramTotal": 4, "fanGpu": None},
    "tablet": {"battery": 64, "charging": True, "batterySecs": None, "batteryPower": 21.3, "batteryHealth": 91,
               "batteryCycles": 143, "batteryDesignWh": 47.7, "batteryFullWh": 43.4, "ramTotal": 15.7, "diskUsed": 211, "diskTotal": 476,
               "igpu": None, "vramTotal": 8, "fanGpu": None},
    "aio": {**_NO_BATTERY, "ramTotal": 15.8, "diskUsed": 236, "diskTotal": 476, "igpu": None, "vramTotal": 8, "fanGpu": None},
    "mini": {**_NO_BATTERY, "ramTotal": 31.6, "diskUsed": 305, "diskTotal": 931, "igpu": None, "vramTotal": 16, "fanGpu": None},
}


def telemetry(d):
    """Overrides the battery and hardware-size fields of a telemetry sample with the fake PC's."""
    t = dict(_TELE[kind() or "desktop"])
    if t.get("vramTotal") and not d.get("vramTotal"):
        t.pop("vramTotal")   # the fake VRAM size only replaces a measured one (no GPU counters: stays unknown)
    d.update(t)
    return d

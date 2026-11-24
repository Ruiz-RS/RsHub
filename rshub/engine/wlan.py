"""The Wi-Fi connection from Windows' Native Wifi API (wlanapi.dll), read inside RsHub's own process.

It replaces `netsh wlan show interfaces`, which the live data started every ten seconds while the window was open
(plus the Network page and Autopilot's "on this Wi-Fi" condition): a stream of short-lived netsh.exe processes asking
about wireless networks is what antivirus behaviour engines watch for (Wi-Fi reconnaissance), and parsing its
localized text needed one key per language. The API answers in about a millisecond, in numbers.

Since Windows 11 24H2 the connection details (SSID, BSSID) need location access; without it Windows answers
"access denied" and current() says needsLocation, like netsh did. The signal is then still read from the RSSI."""
import ctypes
from ctypes import wintypes

ERROR_ACCESS_DENIED = 5
CONNECTED = 1                                     # wlan_interface_state_connected
OP_CURRENT_CONNECTION, OP_CHANNEL, OP_RSSI = 7, 8, 0x10000102

RADIO = {4: "802.11a", 5: "802.11b", 6: "802.11g", 7: "802.11n", 8: "802.11ac", 9: "802.11ad", 10: "802.11ax",
         11: "802.11be"}                          # DOT11_PHY_TYPE
AUTH = {1: "Open", 2: "Shared", 3: "WPA-Enterprise", 4: "WPA-Personal", 5: "WPA-None", 6: "WPA2-Enterprise",
        7: "WPA2-Personal", 8: "WPA3-Enterprise 192 Bits", 9: "WPA3-Personal", 10: "OWE", 11: "WPA3-Enterprise"}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _IFACE(ctypes.Structure):                   # WLAN_INTERFACE_INFO
    _fields_ = [("guid", _GUID), ("desc", ctypes.c_wchar * 256), ("state", ctypes.c_int)]


class _IFACE_LIST(ctypes.Structure):              # WLAN_INTERFACE_INFO_LIST (header; the items follow)
    _fields_ = [("count", wintypes.DWORD), ("index", wintypes.DWORD)]


class _SSID(ctypes.Structure):                    # DOT11_SSID
    _fields_ = [("len", wintypes.ULONG), ("ssid", ctypes.c_ubyte * 32)]


class _ASSOC(ctypes.Structure):                   # WLAN_ASSOCIATION_ATTRIBUTES
    _fields_ = [("ssid", _SSID), ("bssType", ctypes.c_int), ("bssid", ctypes.c_ubyte * 6), ("phy", ctypes.c_int),
                ("phyIndex", wintypes.ULONG), ("quality", wintypes.ULONG), ("rx", wintypes.ULONG), ("tx", wintypes.ULONG)]


class _SECURITY(ctypes.Structure):                # WLAN_SECURITY_ATTRIBUTES
    _fields_ = [("enabled", wintypes.BOOL), ("oneX", wintypes.BOOL), ("auth", ctypes.c_int), ("cipher", ctypes.c_int)]


class _CONNECTION(ctypes.Structure):              # WLAN_CONNECTION_ATTRIBUTES
    _fields_ = [("state", ctypes.c_int), ("mode", ctypes.c_int), ("profile", ctypes.c_wchar * 256), ("assoc", _ASSOC),
                ("security", _SECURITY)]


class _BSS(ctypes.Structure):                     # WLAN_BSS_ENTRY
    _fields_ = [("ssid", _SSID), ("phyId", wintypes.ULONG), ("bssid", ctypes.c_ubyte * 6), ("bssType", ctypes.c_int),
                ("phy", ctypes.c_int), ("rssi", wintypes.LONG), ("linkQuality", wintypes.ULONG), ("inRegDomain", ctypes.c_ubyte),
                ("beaconPeriod", wintypes.USHORT), ("timestamp", ctypes.c_ulonglong), ("hostTimestamp", ctypes.c_ulonglong),
                ("capability", wintypes.USHORT), ("frequency", wintypes.ULONG), ("rateLen", wintypes.ULONG),
                ("rates", wintypes.USHORT * 126), ("ieOffset", wintypes.ULONG), ("ieSize", wintypes.ULONG)]


class _BSS_LIST(ctypes.Structure):                # WLAN_BSS_LIST (header; the entries follow)
    _fields_ = [("size", wintypes.DWORD), ("count", wintypes.DWORD)]


_api = None


def _lib():
    """wlanapi.dll with its prototypes, or None (Windows Server without the Wireless LAN feature)."""
    global _api
    if _api is None:
        try:
            L = ctypes.WinDLL("wlanapi.dll")
        except OSError:
            _api = False
            return None
        P, VP = ctypes.POINTER, ctypes.c_void_p
        L.WlanOpenHandle.argtypes = [wintypes.DWORD, VP, P(wintypes.DWORD), P(wintypes.HANDLE)]
        L.WlanCloseHandle.argtypes = [wintypes.HANDLE, VP]
        L.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, VP, P(VP)]
        L.WlanQueryInterface.argtypes = [wintypes.HANDLE, P(_GUID), ctypes.c_int, VP, P(wintypes.DWORD), P(VP), VP]
        L.WlanGetNetworkBssList.argtypes = [wintypes.HANDLE, P(_GUID), P(_SSID), ctypes.c_int, wintypes.BOOL, VP, P(VP)]
        L.WlanFreeMemory.argtypes = [VP]
        for f in ("WlanOpenHandle", "WlanCloseHandle", "WlanEnumInterfaces", "WlanQueryInterface", "WlanGetNetworkBssList"):
            getattr(L, f).restype = wintypes.DWORD
        L.WlanFreeMemory.restype = None
        _api = L
    return _api or None


def _query(L, h, guid, op, kind):
    """One WlanQueryInterface value as a copy of `kind`, or the Windows error code (an int)."""
    size, ptr = wintypes.DWORD(), ctypes.c_void_p()
    rc = L.WlanQueryInterface(h, ctypes.byref(guid), op, None, ctypes.byref(size), ctypes.byref(ptr), None)
    if rc:
        return rc
    try:
        if size.value < ctypes.sizeof(kind):
            return -1
        return kind.from_buffer_copy(ctypes.string_at(ptr.value, ctypes.sizeof(kind)))
    finally:
        L.WlanFreeMemory(ptr)


def band(mhz=None, channel=None):
    """"2.4 GHz", "5 GHz" or "6 GHz" from the centre frequency (MHz), else guessed from the channel number."""
    if mhz:
        return "2.4 GHz" if mhz < 3000 else "5 GHz" if mhz < 5925 else "6 GHz"
    if channel:
        return "2.4 GHz" if channel <= 14 else "5 GHz"
    return None


def quality_from_rssi(rssi):
    """Windows' signal quality (0-100 %) for an RSSI in dBm: -100 dBm is 0 %, -50 dBm and above is 100 %."""
    return max(0, min(100, 2 * (int(rssi) + 100)))


def _frequency(L, h, guid, conn):
    """The connected access point's centre frequency in MHz, from Windows' cached scan (no new scan), or None."""
    ptr = ctypes.c_void_p()
    ssid = _SSID.from_buffer_copy(bytes(conn.assoc.ssid))
    if L.WlanGetNetworkBssList(h, ctypes.byref(guid), ctypes.byref(ssid), conn.assoc.bssType, conn.security.enabled,
                               None, ctypes.byref(ptr)) or not ptr.value:
        return None
    try:
        head = _BSS_LIST.from_address(ptr.value)
        first = ptr.value + ctypes.sizeof(_BSS_LIST)
        first += (-first) % ctypes.alignment(_BSS)
        for i in range(head.count):
            e = _BSS.from_address(first + i * ctypes.sizeof(_BSS))
            if bytes(e.bssid) == bytes(conn.assoc.bssid) and e.frequency:
                return e.frequency // 1000  # kHz
    finally:
        L.WlanFreeMemory(ptr)
    return None


def current(details=True):
    """The Wi-Fi connection, like `netsh wlan show interfaces` gave it (details=False skips the band and channel, which
    take most of the time, for callers that only want the signal or the name):
      {"connected": True, "ssid", "signal" (0-100), "band", "channel", "radio", "rx", "tx" (Mbps, as text), "auth"}
      {"connected": False, "needsLocation": bool[, "signal"]} when no Wi-Fi is connected, or Windows keeps the
          connection's details behind location access (Windows 11 24H2 and later);
      None when Windows can't say (no Wi-Fi API or its service is stopped)."""
    L = _lib()
    if L is None:
        return None
    h, ver = wintypes.HANDLE(), wintypes.DWORD()
    try:
        if L.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h)):
            return None
    except OSError:
        return None
    lst = ctypes.c_void_p()
    try:
        if L.WlanEnumInterfaces(h, None, ctypes.byref(lst)) or not lst.value:
            return None
        try:
            head = _IFACE_LIST.from_address(lst.value)
            ifaces = [_IFACE.from_buffer_copy(ctypes.string_at(lst.value + ctypes.sizeof(_IFACE_LIST) + i * ctypes.sizeof(_IFACE),
                                                               ctypes.sizeof(_IFACE))) for i in range(head.count)]
        finally:
            L.WlanFreeMemory(lst)
        up = [i for i in ifaces if i.state == CONNECTED]
        if not up:
            return {"connected": False, "needsLocation": False}
        guid = up[0].guid
        conn = _query(L, h, guid, OP_CURRENT_CONNECTION, _CONNECTION)
        if isinstance(conn, int):
            out = {"connected": False, "needsLocation": conn == ERROR_ACCESS_DENIED}
            rssi = _query(L, h, guid, OP_RSSI, wintypes.LONG)
            if not isinstance(rssi, int) and rssi.value < 0:
                out["signal"] = quality_from_rssi(rssi.value)
            return out
        a = conn.assoc
        ssid = bytes(a.ssid.ssid[:min(a.ssid.len, 32)]).decode("utf-8", "replace")
        if not ssid:
            return {"connected": False, "needsLocation": False}
        channel = mhz = None
        if details:
            ch = _query(L, h, guid, OP_CHANNEL, wintypes.ULONG)
            channel = ch.value if not isinstance(ch, int) and ch.value else None
            try:
                mhz = _frequency(L, h, guid, conn)
            except (OSError, ValueError):
                mhz = None
        return {"connected": True, "ssid": ssid, "signal": int(a.quality), "band": band(mhz, channel),
                "channel": str(channel) if channel else None, "radio": RADIO.get(a.phy),
                "rx": str(round(a.rx / 1000)) if a.rx else None, "tx": str(round(a.tx / 1000)) if a.tx else None,
                "auth": AUTH.get(conn.security.auth)}
    except (OSError, ValueError):
        return None
    finally:
        L.WlanCloseHandle(h, None)


def signal():
    """The Wi-Fi signal (0-100 %) of the connected network, or None."""
    w = current(details=False)
    return w.get("signal") if w else None


def ssid():
    """The name of the connected Wi-Fi network, or None."""
    w = current(details=False)
    return w.get("ssid") if w else None

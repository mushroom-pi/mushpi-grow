import utime as time

# ── Software version ──────────────────────────────────────────────
# Bump on every release.  Single source of truth for the firmware
# version (returned by GET /system and GET /).
_SOFTWARE_VERSION = "0.5.0"

# global-ish dictionaries the whole app can import
setpoints = {"temperature": 25, "humidity": 60}
status = {
    "temperature": None, "humidity": None,
    "fan": False, "humidifier": False, "heater": False,
    "last_sensor_ok_at": None, "last_sensor_error": None,
}
boot_ms = time.ticks_ms()  # set at runtime
devices = {
    "pins": { "dht": 4, "humidifier": 6, "fan": 7, "heater": 8 },
    "active_high": False,
}

_system_info = None
software_info = None

def init_software_info(cfg=None):
    name = None
    if cfg: name = cfg["device_name"]

    global software_info
    if software_info is not None:
        return software_info
    software_info = {"version": _SOFTWARE_VERSION, "device_name": name}
    return software_info

def init_system_info(cfg=None):
    """Collect static system info once. Safe to call multiple times."""
    global _system_info
    if _system_info is not None:
        return _system_info

    try:
        import os
    except ImportError:
        import uos as os
    import sys

    # Optional machine helpers
    try:
        from machine import freq
    except Exception:
        freq = None

    # MicroPython impl/version
    name, ver_tuple, ver_str, mpy_tag = "micropython", None, None, None
    try:
        impl = sys.implementation
        name = getattr(impl, "name", name)
        ver_tuple = getattr(impl, "version", None)
        if ver_tuple:
            ver_str = ".".join(str(x) for x in ver_tuple)
        mpy_tag = getattr(impl, "mpy", None)
    except:
        pass

    # Board/port/build strings
    board = port = build = None
    try:
        u = os.uname()           # e.g., rp2 port
        board = getattr(u, "machine", None)   # "Raspberry Pi Pico W with RP2040"
        port  = getattr(u, "sysname", None)   # "rp2"
        build = getattr(u, "version", None)   # firmware build info
    except:
        pass

    # CPU MHz (static for most builds)
    mhz = None
    try:
        f = freq()
        if isinstance(f, int):
            mhz = f // 1_000_000
    except:
        pass

    _system_info = {
        "software": init_software_info(cfg),
        "micropython": {
            "name": name,
            "version": ver_str,
            "version_tuple": ver_tuple,
            "mpy": mpy_tag,
            "build": build or sys.version,
        },
        "hardware": {
            "platform": getattr(sys, "platform", None),  # "rp2"
            "board": board,   # "Raspberry Pi Pico W with RP2040"
            "port": port,     # "rp2"
            "cpu": {"freq_mhz": mhz},
        }
    }
    return _system_info

def attach_wlan_info(wlan, cfg=None):
    """Optionally add MAC once Wi-Fi exists (static enough to cache)."""
    global _system_info
    if _system_info is None:
        init_system_info()
    mac_hex = None
    ip = None
    port = None
    try:
        mac = wlan.config('mac')  # Pico W
        hostname = wlan.config('hostname')
        ip = wlan.ifconfig()[0]
        mac_hex = ":".join("%02x" % b for b in mac)
        if cfg: port = cfg["api_port"]
    except:
        pass
    _system_info["wifi"] = {"mac": mac_hex, "ip": ip, "port": port, "hostname": hostname}

_MDNS_MIN_VERSION = (1, 26, 0)

def check_mdns_firmware(device_name=None):
    """Warn at boot if MicroPython is too old for <hostname>.local mDNS.
    Non-fatal: only prints. See README 'Firmware requirements'.
    Triggered when: version tuple < (1,26,0) OR release/build string
    contains 'preview'/'dirty' (catches pre-release builds like
    v1.25.0-preview that lack the mDNS fix)."""
    import sys
    vt = None
    try:
        v = getattr(sys.implementation, "version", None)
        if v and len(v) >= 2:
            vt = (int(v[0]), int(v[1]), int(v[2]) if len(v) >= 3 else 0)
    except:
        pass

    build = None
    try:
        import os
        build = os.uname().release
        if not build:
            build = os.uname().version
    except:
        pass

    preview = False
    if build:
        b = build.lower()
        preview = ("preview" in b) or ("dirty" in b)

    too_old = (vt is not None) and (vt < _MDNS_MIN_VERSION)

    if too_old or preview:
        ver_str = ".".join(str(x) for x in vt) if vt else (build or "unknown")
        name = device_name or "<device_name>"
        print("============================================================")
        print("WARNING: MicroPython firmware %s is too old for mDNS." % ver_str)
        print("Reaching this unit as %s.local will NOT work." % name)
        print("The rp2/CYW43 mDNS responder was fixed in MicroPython")
        print("v1.25.0 stable (PR micropython/micropython#17057).")
        print("Recommended: reflash to MicroPython >= v1.26.0 (the version")
        print("confirmed working). See README 'Firmware requirements'.")
        print("The DHCP hostname and the REST API on the unit's IP still work.")
        if preview:
            print("Detected a pre-release/preview build - these are not supported.")
        print("============================================================")

def get_system_info():
    """Read-only fetch (returns the cached dict)."""
    return _system_info if _system_info is not None else init_system_info()

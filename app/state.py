import utime as time

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
control_enabled = True

_system_info = None
software_info = None

def init_software_info_from_file(cfg=None, path="VERSION"):
    name = None
    if cfg: name = cfg["device_name"]

    global software_info
    if software_info is not None:
        return software_info
    ver = "0.0.0"
    try:
        with open(path, "r") as f:
            ver = f.read().strip()
    except Exception:
        pass
    software_info = {"version": ver, "device_name": name}
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
        "software": init_software_info_from_file(cfg),
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
        ip = wlan.ifconfig()[0]
        mac_hex = ":".join("%02x" % b for b in mac)
        if cfg: port = cfg["api_port"]
    except:
        pass
    _system_info["wifi"] = {"mac": mac_hex, "ip": ip, "port": port}

def get_system_info():
    """Read-only fetch (returns the cached dict)."""
    return _system_info if _system_info is not None else init_system_info()

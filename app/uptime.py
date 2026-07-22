import time
import ujson
import machine

# os/uos compatibility
try:
    import os
except ImportError:
    import uos as os

# Reset cause constants — rp2 port only exposes PWRON_RESET and WDT_RESET
# as named attributes on the machine module. Fall back to raw integer
# values for ports that don't expose all constants.
_PWRON_RESET = getattr(machine, "PWRON_RESET", 1)
_WDT_RESET = getattr(machine, "WDT_RESET", 3)

# Module state
_cumulative_ms = 0
_reboot_reason = "unknown"
_boot_ms = 0
_UPTIME_PATH = "uptime.json"

# Reboot types that preserve cumulative uptime (vs reset to 0)
_PRESERVE_TYPES = ("scheduled", "soft")


def _atomic_write(path, data):
    """Atomically write JSON data to path using .tmp + os.rename."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            ujson.dump(data, f)
            f.flush()
        os.rename(tmp, path)
    except Exception:
        pass  # best-effort, never crash on flash write failures


def _load_file():
    """Load uptime.json as a dict, or empty dict on any error."""
    try:
        with open(_UPTIME_PATH, "r") as f:
            return ujson.load(f)
    except Exception:
        return {}


def mark_pending_reboot(reboot_type):
    """Write sentinel to uptime.json before an intentional reboot.
    
    reboot_type: "scheduled" | "soft" | "hard"
    
    - "scheduled"/"soft": cumulative uptime is preserved across the reboot.
    - "hard": cumulative uptime is reset to 0 (full power-cycle equivalent).
    - "scheduled" also stamps today's date to prevent same-day double-trigger.
    
    Call this RIGHT before machine.reset() / machine.soft_reset().
    On the next boot, init_uptime() reads the sentinel and classifies
    the reboot correctly regardless of what reset_cause() reports.
    """
    existing = _load_file()
    now_ms = time.ticks_ms()

    # Accumulate current session into cumulative
    session_ms = time.ticks_diff(now_ms, _boot_ms)
    prev = existing.get("cumulative_ms", 0)
    total = prev + max(session_ms, 0)

    # For hard reboots, don't carry cumulative forward
    if reboot_type not in _PRESERVE_TYPES:
        total = 0

    data = {
        "cumulative_ms": total,
        "reboot_done_date": existing.get("reboot_done_date"),
        "pending_type": reboot_type,
    }

    # Stamp today's date for scheduled reboots (prevents same-day double-trigger)
    if reboot_type == "scheduled":
        data["reboot_done_date"] = today_str()

    _atomic_write(_UPTIME_PATH, data)


def init_uptime():
    """Called once at boot (before WiFi). Classifies reboot reason and
    loads/initialises cumulative uptime."""
    global _cumulative_ms, _reboot_reason, _boot_ms

    _boot_ms = time.ticks_ms()
    data = _load_file()

    # 1. Check sentinel first — intentional reboots set pending_type
    pending = data.get("pending_type")
    if pending is not None:
        _reboot_reason = pending
        if pending in _PRESERVE_TYPES:
            _cumulative_ms = data.get("cumulative_ms", 0)
        else:
            _cumulative_ms = 0
        # Clear the sentinel so an unexpected reboot doesn't inherit it
        data["pending_type"] = None
        _atomic_write(_UPTIME_PATH, data)
        return

    # 2. No sentinel → fall back to reset_cause() for unexpected reboots
    cause = machine.reset_cause()
    if cause == _WDT_RESET:
        _reboot_reason = "watchdog"
    elif cause == _PWRON_RESET:
        _reboot_reason = "power_on"
    else:
        _reboot_reason = "unknown"
    _cumulative_ms = 0

    # Write fresh state (carries forward reboot_done_date as same-day guard)
    _atomic_write(_UPTIME_PATH, {
        "cumulative_ms": 0,
        "reboot_done_date": data.get("reboot_done_date"),
        "pending_type": None,
    })


def reboot_done_date():
    """Return the last reboot_done_date string, or None."""
    data = _load_file()
    return data.get("reboot_done_date") or None


def today_str():
    """Return current date as 'YYYY-MM-DD' from RTC.
    Falls back to empty string if RTC not sync'd."""
    try:
        t = time.localtime()
        return "%04d-%02d-%02d" % (t[0], t[1], t[2])
    except Exception:
        return ""


def get_uptime_info():
    """Return uptime dict suitable for API responses."""
    now_ms = time.ticks_ms()
    session_ms = time.ticks_diff(now_ms, _boot_ms)
    total_ms = _cumulative_ms + session_ms
    return {
        "total_s": max(total_ms, 0) // 1000,
        "since_boot_s": max(session_ms, 0) // 1000,
        "reboot_reason": _reboot_reason,
    }

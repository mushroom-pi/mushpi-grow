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
_SOFT_RESET = getattr(machine, "SOFT_RESET", 4)
_WDT_RESET = getattr(machine, "WDT_RESET", 3)

# Module state
_cumulative_ms = 0
_reboot_reason = "unknown"
_boot_ms = 0
_UPTIME_PATH = "uptime.json"


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


def _load_done_date():
    """Load the reboot_done_date from uptime.json, or None."""
    try:
        with open(_UPTIME_PATH, "r") as f:
            data = ujson.load(f)
        return data.get("reboot_done_date", None)
    except Exception:
        return None


def _save_state():
    """Save current cumulative uptime + done_date to uptime.json."""
    data = {"cumulative_ms": _cumulative_ms, "reboot_done_date": _load_done_date()}
    _atomic_write(_UPTIME_PATH, data)


def init_uptime():
    """Called once at boot (before WiFi). Classifies reboot reason and
    loads/initialises cumulative uptime."""
    global _cumulative_ms, _reboot_reason, _boot_ms

    cause = machine.reset_cause()
    _boot_ms = time.ticks_ms()

    if cause == _SOFT_RESET:
        _reboot_reason = "scheduled"
        try:
            with open(_UPTIME_PATH, "r") as f:
                data = ujson.load(f)
            _cumulative_ms = data.get("cumulative_ms", 0)
        except Exception:
            _cumulative_ms = 0
    elif cause == _WDT_RESET:
        _reboot_reason = "watchdog"
        _cumulative_ms = 0
    elif cause == _PWRON_RESET:
        _reboot_reason = "power_on"
        _cumulative_ms = 0
    else:
        _reboot_reason = "unknown"
        _cumulative_ms = 0

    # Write fresh uptime.json — carries forward reboot_done_date as the
    # same-day guard so the scheduler does not re-trigger today.
    _save_state()


def reboot_done_date():
    """Return the last reboot_done_date string, or None."""
    return _load_done_date()


def persist_cumulative():
    """Persist current cumulative uptime + today's date to uptime.json.
    Called right before machine.soft_reset()."""
    now_ms = time.ticks_ms()
    total = _cumulative_ms + time.ticks_diff(now_ms, _boot_ms)
    today = today_str()
    data = {"cumulative_ms": max(total, 0), "reboot_done_date": today}
    _atomic_write(_UPTIME_PATH, data)


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

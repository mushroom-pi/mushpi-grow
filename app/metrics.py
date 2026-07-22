import gc, utime as time
import uasyncio as asyncio

from .state import boot_ms

# os/uos compatibility
try:
    import os
except ImportError:
    import uos as os

# machine bits (may vary by port)
try:
    from machine import freq, ADC
except Exception:
    def freq(): return None
    ADC = None

# ---- simple, non-blocking event-loop "load" meter ----
_best_idle = 0
_last_util_pct = None  # 0..100 (None until measured)

async def _idle_meter(period_ms=1000):
    """Measures how many zero-delay yields fit in period_ms.
       Lower count => busier loop => higher utilization%."""
    global _best_idle, _last_util_pct
    while True:
        end = time.ticks_add(time.ticks_ms(), period_ms)
        idle_loops = 0
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            await asyncio.sleep_ms(0)  # cooperative yield
            idle_loops += 1
        if idle_loops > _best_idle:
            _best_idle = idle_loops
        if _best_idle:
            util = 1.0 - (idle_loops / _best_idle)
            if util < 0: util = 0.0
            _last_util_pct = int(util * 100)

def start_metrics():
    """Call once from inside your running event loop (e.g., in main())."""
    asyncio.create_task(_idle_meter())

# ---- helpers ----
def _wifi_rssi(wlan):
    try:
        val = wlan.status('rssi')  # supported on Pico W’s CYW43 in recent builds
        return int(val) if isinstance(val, int) else None
    except:
        return None
    
def _wifi(wlan):
    wifi_ok = bool(wlan.isconnected())

    return {
        "connected": wifi_ok,
        "rssi": _wifi_rssi(wlan) # dBm (negative), or None
    }

def _mcu_temp_c():
    if ADC is None:
        return None
    try:
        sensor = ADC(4)  # RP2040 internal temp sensor
        conv = 3.3 / 65535
        v = sensor.read_u16() * conv
        # Datasheet nominal formula (rough, not calibrated)
        return round((27 - (v - 0.706) / 0.001721) * 10) / 10
    except:
        return None

def _fs_usage(path='/'):
    try:
        s = os.statvfs(path)
        total = s[0] * s[2]
        free  = s[0] * s[3]
        used  = total - free
        used_pct = int((used * 100) / total) if total else None
        return {"total": total, "used": used, "free": free, "used_pct": used_pct}
    except:
        return {"total": None, "used": None, "free": None, "used_pct": None}
    
def _up_time_s():
    """Return cumulative total uptime in seconds (preserved across scheduled reboots)."""
    try:
        from . import uptime
        return uptime.get_uptime_info()["total_s"]
    except Exception:
        # Fallback to session uptime if uptime module not available
        uptime_ms = time.ticks_diff(time.ticks_ms(), boot_ms)
        return max(0, uptime_ms // 1000)

def system_snapshot(wlan=None):
    # Heap
    free = gc.mem_free()
    used = gc.mem_alloc()
    total = free + used
    mem = {
        "total": total,
        "used": used,
        "free": free,
        "used_pct": int((used * 100) / total) if total else None,
    }

    return {
        "mem": mem,                # bytes
        "fs": _fs_usage("/"),      # bytes
        "wifi": _wifi(wlan),
        "mcu_temp_c": _mcu_temp_c(),        # rough, may be None
        "event_loop_util_pct": _last_util_pct,  # 0..100, None until first window elapses,
        "uptime_s": _up_time_s() # up time in seconds
    }

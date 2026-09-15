from app.metrics import system_snapshot
from app.state import get_system_info
import usocket
import uasyncio as asyncio
import ujson

from .state import setpoints, _API_VERSION

# Use MicroPython's urequests (upload to /lib if missing)
try:
    import urequests as requests
except ImportError:
    requests = None  # we'll raise a clear error on use


def _ip(wlan):
    try:
        return wlan.ifconfig()[0]
    except:
        return None


def payload(wlan, extra=None):
    system = get_system_info()
    health = system_snapshot(wlan)
    data = {
        "handle": system["software"]["device_name"],
        "ip": system['wifi']['ip'],
        "mac": system['wifi']['mac'],
        "port": system['wifi']['port'],
        "micropython_version": system["micropython"]["build"],
        "firmware_version": system["software"]["version"],
        "api_version": _API_VERSION,
        "board": system["hardware"]["board"],
        "board_total_mem_byte": health["mem"]["total"],
        "board_total_fs_byte": health["fs"]["total"],
        "board_cpu_freq_mhz": system["hardware"]["cpu"]["freq_mhz"]
    }

    if extra and isinstance(extra, dict):
        data.update(extra)
    return data


def announce_once_blocking(cfg, wlan, io=None, extra=None, timeout_s=2):
    """
    One-shot POST to hub using urequests. Blocking (for up to timeout_s).
    Returns True on 2xx, False otherwise.
    """
    if requests is None:
        raise RuntimeError(
            "urequests not found. Upload urequests.py to /lib on the Pico.")

    # Must have Wi-Fi & IP
    if not wlan or not (hasattr(wlan, "isconnected") and wlan.isconnected()) or not _ip(wlan):
        return False

    # Build payload
    body = payload(wlan, extra)

    # Set a small global socket timeout for this call; restore after
    try:
        usocket.setdefaulttimeout(timeout_s)
    except:
        pass

    ok = False
    try:
        # urequests silently ignores the `json=` kwarg — pass data as a
        # pre-serialised string so the non-dict branch in URLOpener fires,
        # which is the only branch that actually appends a body + Content-Length.
        # Do NOT include Content-Length in the headers dict: URLOpener adds it
        # itself on line 52, so a duplicate would cause body-parser to reject
        # the body.
        body_str = ujson.dumps(body)
        secret = cfg.get("hub_secret", "") if isinstance(cfg, dict) else ""
        r = requests.post(
            cfg["hub_url"],
            data=body_str,
            headers={
                "Content-Type": "application/json",
                "Connection": "close",
                "X-Pico-Secret": secret,
            }
        )

        code = getattr(r, "status_code", None)
        ok = (code is not None) and (200 <= int(code) < 300)
        if ok:
            print("announce: ok")
        else:
            print("announce: fail (status {})".format(code))
    except Exception as e:
        print("announce: exception:", e)
        ok = False
    finally:
        try:
            # Restore default (blocking) timeout
            usocket.setdefaulttimeout(None)
        except:
            pass

    return ok


async def announce_then_retry_once(cfg, wlan, io=None, delay_s=60, timeout_s=2, stop_event=None):
    """
    Try once now (blocking up to timeout_s). If it fails, wait delay_s and try once more.
    Returns True if any attempt succeeded.
    NOTE: Each attempt blocks the event loop briefly (<= timeout_s).
    """
    if io and hasattr(io, "led_solid"):
        io.led_solid(True)

    ok = announce_once_blocking(cfg, wlan, io, timeout_s=timeout_s)
    if ok:
        if io:
            io.led_flash(cycles=2, period_ms=120)
            io.start_led_heartbeat(period_ms=800, stop_event=stop_event)
        return True

    # wait, but stay cancelable
    remaining = int(delay_s)
    while remaining > 0:
        if stop_event and stop_event.is_set():
            return False
        await asyncio.sleep(1)
        remaining -= 1

    # SECOND attempt: handle LED just like the first
    ok = announce_once_blocking(cfg, wlan, timeout_s=timeout_s)
    if ok:
        if io:
            io.led_flash(cycles=2, period_ms=120)
            io.start_led_heartbeat(period_ms=800, stop_event=stop_event)
        return True

    # both failed → stay solid ON
    if io and hasattr(io, "led_solid"):
        io.led_solid(True)
    return False

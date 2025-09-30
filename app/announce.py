import usocket
import uasyncio as asyncio

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


def payload(cfg, wlan, extra=None):
    data = {
        "name": cfg["device_name"],
        "host": _ip(wlan),
        "port": cfg["api_port"],
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
        raise RuntimeError("urequests not found. Upload urequests.py to /lib on the Pico.")

    # Must have Wi-Fi & IP
    if not wlan or not (hasattr(wlan, "isconnected") and wlan.isconnected()) or not _ip(wlan):
        return False

    # Build payload
    body = payload(cfg, wlan, extra)

    # Set a small global socket timeout for this call; restore after
    try:
        usocket.setdefaulttimeout(timeout_s)
    except:
        pass

    ok = False
    try:
        # Connection: close so sockets are freed promptly
        r = requests.post(cfg["hub_url"], json=body, headers={"Connection": "close"})
        # Read/close to release socket
        try:
            _ = r.text
        finally:
            r.close()

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

import uasyncio as asyncio
import usocket

# Use MicroPython's lightweight HTTP client
try:
    import urequests as requests
except ImportError:
    requests = None  # We'll raise a clear error if it's missing


def _ip(wlan):
    try:
        return wlan.ifconfig()[0]
    except:
        return None


def payload(cfg, wlan, extra=None):
    data = {
        "name": cfg["device_name"],
        "ip": _ip(wlan),
        "port": cfg["api_port"],
        "capabilities": ["status", "setpoints", "health", "probe"],
    }
    if extra and isinstance(extra, dict):
        data.update(extra)
    return data


def announce_once(cfg, wlan, extra=None, timeout_s=3):
    """
    One-shot POST to the hub. Returns True on success, False otherwise.
    Uses urequests with json= (so Content-Type is set).
    """
    if requests is None:
        raise RuntimeError(
            "urequests not found. Upload urequests.py to /lib on the Pico."
        )

    ip = _ip(wlan)
    if not ip:
        print("announce: no IP yet")
        return False

    body = payload(cfg, wlan, extra)
    # Some urequests builds don't honor per-request timeout;
    # set a global default to avoid hanging sockets.
    try:
        usocket.setdefaulttimeout(timeout_s)
    except:
        pass

    try:
        r = requests.post(cfg["hub_url"], json=body)
        # Read/close to free the socket
        _ = r.text
        r.close()
        print("announce: ok", body.get("ip"))
        return True
    except Exception as e:
        print("announce: fail:", e)
        return False


async def announce_loop(cfg, wlan, io=None, interval_s=60, first_delay_s=2):
    """
    Periodic heartbeat: announces at startup (after first_delay_s),
    then every interval_s; on failure, retries sooner (10s).
    """
    await asyncio.sleep(first_delay_s)
    while True:
        ok = announce_once(cfg, wlan)
        if ok and io and hasattr(io, "led_blink"): io.led_blink()
        await asyncio.sleep(10 if not ok else interval_s)


async def announce_on_boot(cfg, wlan, tries=3, gap_s=2):
    """
    Optional helper: call once from main() to try a few quick announces
    right after Wi-Fi connects, before starting the loop.
    """
    for i in range(tries):
        if announce_once(cfg, wlan):
            return True
        await asyncio.sleep(gap_s)
    return False

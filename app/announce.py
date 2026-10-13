from app.metrics import system_snapshot
from app.state import get_system_info
import uasyncio as asyncio
import ujson

from .state import setpoints, _API_VERSION

# Use MicroPython's urequests (upload to /lib if missing)
try:
    import urequests as requests
except ImportError:
    requests = None  # we'll raise a clear error on use

# Registration path on the hub. This is firmware knowledge, not operator
# configuration: config.json's hub_url carries the hub address only and the
# firmware appends this path itself. It lives here because this module owns
# how announcing is done.
_ANNOUNCE_PATH = "/v1/pico-units/announce"


def _ip(wlan):
    try:
        return wlan.ifconfig()[0]
    except:
        return None


def _strip_trailing_slashes(s):
    # Slice loop instead of str.removesuffix(): that method does not exist in
    # MicroPython, so peel trailing "/" characters off the end one at a time.
    while len(s) and s[-1] == "/":
        s = s[:-1]
    return s


def announce_url(raw):
    """
    Resolve the full announce URL from the configured hub value.

    config.json's "hub_url" is meant to carry the hub's address only (scheme,
    host, port); the firmware appends _ANNOUNCE_PATH itself. A value that
    already ends in the announce path is accepted unchanged, deliberately:
    units flashed with the older full-endpoint form keep working without
    being re-flashed. Surrounding whitespace and trailing slashes are
    tolerated. Non-string or empty input yields "" (no announce target).
    """
    if not isinstance(raw, str):
        return ""
    base = _strip_trailing_slashes(raw.strip())
    if not base:
        return ""
    if base[-len(_ANNOUNCE_PATH):] == _ANNOUNCE_PATH:
        print("announce: hub_url already includes the announce path — using it as configured")
        return base
    return base + _ANNOUNCE_PATH


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


def announce_once_blocking(cfg, wlan, io=None, extra=None):
    """
    One-shot POST to hub using urequests. Blocking, bounded only by the HTTP
    client's own fixed per-socket timeout (urequests hardcodes 5 s); there is
    no configurable announce timeout. Returns True on 2xx, False otherwise.
    """
    if requests is None:
        raise RuntimeError(
            "urequests not found. Upload urequests.py to /lib on the Pico.")

    # Resolve the hub target from the configured address. Done before the
    # Wi-Fi check so a misconfigured hub is visible even when the link is down.
    target = announce_url(cfg.get("hub_url") if isinstance(cfg, dict) else None)
    if not target:
        print("announce: skipped (hub_url not configured)")
        return False

    # Must have Wi-Fi & IP
    if not wlan or not (hasattr(wlan, "isconnected") and wlan.isconnected()) or not _ip(wlan):
        return False

    # Build payload
    body = payload(wlan, extra)

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
            target,
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
            # Include the resolved target so a wrong address (which yields a
            # "not found" from the hub) is distinguishable from a hub outage.
            print("announce: fail (status {}) target {}".format(code, target))
    except Exception as e:
        print("announce: exception:", e)
        ok = False

    return ok


async def announce_then_retry_once(cfg, wlan, io=None, delay_s=60, stop_event=None):
    """
    Try once now (blocking; see announce_once_blocking for the timeout).
    If it fails, wait delay_s and try once more.
    Returns True if any attempt succeeded.
    NOTE: Each attempt blocks the event loop for the HTTP client's fixed
    socket timeout — there is no configurable announce timeout.
    """
    if io and hasattr(io, "led_solid"):
        io.led_solid(True)

    ok = announce_once_blocking(cfg, wlan, io)
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
    ok = announce_once_blocking(cfg, wlan)
    if ok:
        if io:
            io.led_flash(cycles=2, period_ms=120)
            io.start_led_heartbeat(period_ms=800, stop_event=stop_event)
        return True

    # both failed → stay solid ON
    if io and hasattr(io, "led_solid"):
        io.led_solid(True)
    return False

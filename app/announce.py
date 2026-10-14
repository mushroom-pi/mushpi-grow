"""
Presence announcement to the hub: this module owns building the payload,
parsing and resolving the hub address, the socket transport, and the retry
loop. main.py spawns announce_then_retry_once() at boot and app/wifi.py spawns
it again on link-up; everything else here is private to this file.

Why the transport is hand-rolled on usocket rather than urequests: urequests
could not be bounded. Its `timeout=` argument is dead code (URLOpener ignores
it and calls s.settimeout(5) itself), lib/urequests.py is gitignored so no
flashed unit carries a version this repo controls, and that hardcoded 5 s
covered only two of the three blocking phases — the name lookup had no timeout
at all, and the client's `while 1: s.recv(...)` loop gave every round a fresh
full timeout, so one announce could stall unboundedly. control_loop is the
watchdog's ONLY feed site, so a stalled announce starves it and the unit
resets. The transport below bounds each phase itself and awaits between slices:
an unreachable hub costs an announce, never regulation.
"""

from app.metrics import system_snapshot
from app.state import get_system_info
import uasyncio as asyncio
import ujson
import utime as time

try:
    import usocket as socket
except ImportError:
    try:
        import socket
    except ImportError:
        socket = None

try:
    import ussl as ssl
except ImportError:
    try:
        import ssl
    except ImportError:
        ssl = None

from .state import _API_VERSION

# ---------------------------------------------------------------------------
# TRANSPORT BUDGETS — every number the announce may block for. Firmware
# constants, deliberately NOT config.json keys: an operator must not be able to
# dial a blocking announce that starves the control loop. Each is measured
# against the 8 s watchdog window (machine.WDT(timeout=8000), built in main.py,
# fed only by control_loop every 200 ms; the rp2 WDT cannot be stood down once
# armed, so lengthening it is never the fix). Biggest single uninterrupted
# block in an attempt: the 3 s connect.
# ---------------------------------------------------------------------------

# Hub registration path. Firmware knowledge, not operator configuration:
# config.json's hub_url carries the hub address only; we append this ourselves.
_ANNOUNCE_PATH = "/v1/pico-units/announce"

# Ceiling on the one blocking connect() call: 3 s < the 8 s window (3/8 of it),
# leaving control_loop room to sample, regulate and feed.
_CONN_TIMEOUT_S = 3.0

# Timeout for each send()/recv() slice: 1 s per slice with an
# `await asyncio.sleep_ms(0)` between slices, so at most 1 s passes before
# control_loop is scheduled again and feeds.
_IO_SLICE_S = 1.0

# Wall-clock cap on the response-header read, because each recv() gets a FRESH
# _IO_SLICE_S: without this the number of slices — and the sum of their blocking
# — would be unbounded. 6 s of sliced, awaited reads.
_RESP_BUDGET_MS = 6000

# Header accumulation: slice size per recv() and a hard cap on what we keep.
# Only the status line is parsed, so a verbose server cannot grow this (1024 /
# 256 = at most four retained slices).
_RECV_CHUNK = 256
_HEAD_CAP = 1024

# OSError errnos meaning "the per-socket timeout expired" (no data yet) rather
# than a hard socket error: both lwip/POSIX numbers and WSA numbers.
_TIMEOUT_ERRNOS = (11, 103, 110, 116, 10035, 10060)

# Seconds before the 2nd..6th attempt; later retries repeat the last entry
# (300 s cap). These waits are pure `await asyncio.sleep()`: they hold nothing,
# so an interval cannot starve the watchdog however long it is.
_RETRY_BACKOFF_S = (30, 30, 60, 120, 300)

# ---------------------------------------------------------------------------
# PAYLOAD & URL CONSTRUCTION — turns config.json's hub_url into the full POST
# target, and the unit's cached identity into the announce body. The body keys
# ARE the hub-side contract (spec/openapi.yaml AnnouncePayload, mirrored by the
# mock and the server DTO), so payload() reads cached state only — never the
# sensor, never a live GPIO.
# ---------------------------------------------------------------------------


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
    """Full announce URL from the configured hub value.

    hub_url is meant to carry the hub address only (scheme, host, port) and the
    firmware appends _ANNOUNCE_PATH itself; a value already ending in the
    announce path is used unchanged, so units flashed with the older
    full-endpoint form keep working without re-flashing. Surrounding whitespace
    and trailing slashes are tolerated. Non-string or empty input yields ""."""
    if not isinstance(raw, str):
        return ""
    base = _strip_trailing_slashes(raw.strip())
    if not base:
        return ""
    if base[-len(_ANNOUNCE_PATH):] == _ANNOUNCE_PATH:
        print("announce: hub_url already includes the announce path — using it as configured")
        return base
    return base + _ANNOUNCE_PATH


def payload(wlan):
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
    return data


# ---------------------------------------------------------------------------
# TARGET PARSING — splits an announce URL into scheme/host/port/path and
# recognises an IPv4 literal. No DNS here; the transport parses on every
# attempt, so this must never raise and never block.
# ---------------------------------------------------------------------------


def _is_dotted_decimal(host):
    """True for an IPv4 literal, so no name lookup is attempted for it."""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p or len(p) > 3 or not p.isdigit():
            return False
        if int(p) > 255:
            return False
    return True


def _parse_target(url):
    """Split an announce URL into (scheme, host, port, path).

    Same interpretation the old urequests transport used: scheme lower-cased,
    default port 80 (http) / 443 (https), path defaults to "/". Returns None
    for anything that is not a scheme://host URL — never raises."""
    try:
        scheme, rest = url.split("://", 1)
        scheme = scheme.lower()
        if "/" in rest:
            hostport, path = rest.split("/", 1)
            path = "/" + path
        else:
            hostport, path = rest, "/"
        if ":" in hostport:
            host, port_s = hostport.split(":", 1)
            port = int(port_s)
        else:
            host = hostport
            port = 443 if scheme == "https" else 80
        if not host:
            return None
        return (scheme, host, port, path)
    except Exception as e:
        print("announce: unparseable hub URL '" + str(url) + "': " + str(e))
        return None


# ---------------------------------------------------------------------------
# HUB ADDRESS CACHE — the only place a name is looked up, at BOOT ONLY.
# getaddrinfo() has no timeout of its own, so it must never run while the
# watchdog is armed: presolve_boot_hub() is called at main.py module scope,
# before WDT() is constructed. The retry path reads this cache and NEVER
# resolves. Consequence, by design: a hub address change, or a boot lookup that
# failed, is picked up at the NEXT boot. Mutated in place, never reassigned.
# ---------------------------------------------------------------------------

# "target" is the announce URL the cached entry belongs to; "addr" is the
# resolved sockaddr, or None when nothing was resolved for that target.
_hub = {"target": "", "addr": None}


def _ensure_resolved(target):
    """Resolve (and cache) the hub address so an attempt needs no lookup.

    A dotted-decimal host is used directly with no DNS; anything else goes
    through getaddrinfo(), which is why the only caller is the boot path.
    Mutates `_hub` in place; returns True when `_hub["addr"]` is usable for
    connect(); never raises."""
    parts = _parse_target(target)
    if parts is None:
        _hub["target"] = ""
        _hub["addr"] = None
        return False

    scheme, host, port, path = parts

    if _hub["target"] != target:
        # New or first-seen hub address: whatever we cached belongs to a
        # different host and must not be reused.
        _hub["target"] = target
        _hub["addr"] = None

    if _hub["addr"] is not None:
        return True

    if _is_dotted_decimal(host):
        _hub["addr"] = (host, port)
        print("announce: resolved " + host + " -> " + host + " (IPv4 literal, no lookup)")
        return True

    if socket is None:
        _hub["addr"] = None
        print("announce: resolve failed for " + host + " (no socket module)")
        return False

    try:
        info = socket.getaddrinfo(host, port)
        addr = info[0][-1]
    except Exception as e:
        # A failed lookup raises OSError; anything stranger is treated alike.
        _hub["addr"] = None
        print("announce: resolve FAILED for " + host + ":" + str(port) + " — " + str(e))
        return False

    if not addr:
        _hub["addr"] = None
        print("announce: resolve FAILED for " + host + ":" + str(port) + " — no address returned")
        return False

    _hub["addr"] = addr
    print("announce: resolved " + host + " -> " + str(addr[0]))
    return True


def presolve_boot_hub(cfg, wlan):
    """Best-effort hub resolution on the boot path, before the event loop has
    anything else to do and before the watchdog exists. Prints what it did,
    never raises, and returns True when an address is cached afterwards."""
    try:
        target = announce_url(cfg.get("hub_url") if isinstance(cfg, dict) else None)
        if not target:
            print("announce: hub presolve skipped (hub_url not configured)")
            return False
        if not wlan or not (hasattr(wlan, "isconnected") and wlan.isconnected()) or not _ip(wlan):
            print("announce: hub presolve skipped (no Wi-Fi link)")
            return False
        print("announce: pre-resolving hub host ...")
        return _ensure_resolved(target)
    except Exception as e:
        print("announce: hub presolve error:", e)
        return False


# ---------------------------------------------------------------------------
# DIAGNOSTICS — what the last attempt cost and where it ended. The serial
# console is the only bench instrument, so exactly one line per attempt carries
# the number, elapsed ms and outcome phase.
# ---------------------------------------------------------------------------

# From the most recent attempt, read by the retry loop's log line. In place.
_last_attempt = {"ms": 0, "phase": "", "status": None}


def _log_attempt(n, ok):
    """One bench-recordable line per attempt: number, elapsed, outcome."""
    status = _last_attempt["status"]
    if status is None:
        detail = "no status (" + (_last_attempt["phase"] or "unknown") + ")"
    else:
        detail = "status " + str(status)
    line = "announce: attempt " + str(n) + " done in " + str(_last_attempt["ms"]) + " ms (" + detail + ")"
    if not ok and _hub["target"]:
        # The target makes a wrong address (a "not found" from the hub)
        # distinguishable from a hub outage.
        line += " target " + _hub["target"]
    print(line)


# ---------------------------------------------------------------------------
# BOUNDED SOCKET TRANSPORT — one POST, written and read by hand so every block
# obeys TRANSPORT BUDGETS above. The only code allowed to block while the
# watchdog is armed is a socket call whose timeout this firmware set itself, and
# every phase boundary awaits.
# ---------------------------------------------------------------------------


def _set_timeout(s, raw, seconds):
    """Apply a per-socket timeout to the wrapper and the raw socket it wraps.

    ussl-wrapped sockets do not necessarily expose settimeout(), so both ends
    are tried and either landing bounds the blocking calls. True if one landed."""
    ok = False
    for so in (s, raw):
        if so is None:
            continue
        try:
            so.settimeout(seconds)
            ok = True
        except:
            pass
    return ok


def _is_timeout(e):
    """True when an OSError is the per-socket slice timeout expiring."""
    try:
        if not e.args:
            return False
        return e.args[0] in _TIMEOUT_ERRNOS
    except:
        return False


async def _post_once_async(cfg, wlan):
    """One cooperative announce POST: True on 2xx, False otherwise.

    The wire format is the one the hub has always received — POST with
    Content-Type, X-Pico-Secret and Connection: close, body pre-serialised with
    Content-Length written exactly once. Only the status line is read; the body
    never is. Bounds are commented inline at each phase below."""
    _last_attempt["ms"] = 0
    _last_attempt["phase"] = "start"
    _last_attempt["status"] = None
    start = time.ticks_ms()

    if socket is None:
        _last_attempt["phase"] = "no socket module"
        print("announce: fail (no socket module)")
        return False

    # Target before the Wi-Fi check, so a misconfigured hub is also visible
    # while the link is down.
    target = announce_url(cfg.get("hub_url") if isinstance(cfg, dict) else None)
    if not target:
        _last_attempt["phase"] = "hub_url not configured"
        print("announce: skipped (hub_url not configured)")
        return False

    if not wlan or not (hasattr(wlan, "isconnected") and wlan.isconnected()) or not _ip(wlan):
        _last_attempt["phase"] = "no Wi-Fi link"
        return False

    parts = _parse_target(target)
    if parts is None:
        _last_attempt["phase"] = "hub URL unparseable"
        return False
    scheme, host, port, path = parts

    # RUNTIME LOOKUP RULE — the retry path never resolves. getaddrinfo() has no
    # timeout of its own, so calling it here would block inside the armed
    # watchdog window; the only lookup is the boot pre-resolve above. Use what
    # boot cached; an IPv4 literal needs no cache (reading one is string work).
    # With neither, this attempt has no address: log and return WITHOUT any
    # lookup and let the backoff reschedule — the address comes at the next
    # boot. Adding a lookup here "just in case" is the bug this prevents.
    addr = _hub["addr"] if _hub["target"] == target else None
    if addr is None and _is_dotted_decimal(host):
        addr = (host, port)
    if addr is None:
        _last_attempt["phase"] = "no cached hub address"
        print("announce: no hub address available for " + host +
              " (boot lookup failed or never ran) — retrying without a lookup;" +
              " an address is only fetched at the next boot")
        return False

    # Content-Length written once, here, as the BYTE length of the body
    # (urequests used the character length and appended its own copy).
    body_bytes = ujson.dumps(payload(wlan)).encode()
    secret = cfg.get("hub_secret", "") if isinstance(cfg, dict) else ""
    request = (
        ("POST " + path + " HTTP/1.0\r\n"
         + "Host: " + host + "\r\n"
         + "Content-Type: application/json\r\n"
         + "X-Pico-Secret: " + str(secret) + "\r\n"
         + "Connection: close\r\n"
         + "Content-Length: " + str(len(body_bytes)) + "\r\n"
         + "\r\n").encode() + body_bytes
    )

    s = None
    raw = None
    try:
        # Ordering mirrors the old transport: for https the SSL layer wraps the
        # socket before connecting. (The https branch is untested on hardware —
        # REFERENCE §Announce transport & watchdog budgets.)
        if scheme == "https":
            if ssl is None:
                _last_attempt["phase"] = "https but no ssl module"
                print("announce: fail (https requested, no ssl module)")
                return False
            try:
                raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM,
                                    getattr(socket, "IPPROTO_SEC", 0))
            except Exception:
                raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _set_timeout(raw, None, _CONN_TIMEOUT_S)
            try:
                s = ssl.wrap_socket(raw)
            except Exception as e:
                _last_attempt["phase"] = "ssl wrap failed"
                print("announce: ssl wrap failed — " + str(e))
                return False
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        if not _set_timeout(s, raw, _CONN_TIMEOUT_S):
            _last_attempt["phase"] = "settimeout unavailable"
            print("announce: fail (could not set a socket timeout)")
            return False

        # THE one large uninterrupted block of an attempt: at most
        # _CONN_TIMEOUT_S (3 s of the 8 s window), then we yield.
        try:
            s.connect(addr)
        except Exception as e:
            # Any connect error is terminal for this attempt.
            _last_attempt["phase"] = "connect failed"
            _last_attempt["ms"] = time.ticks_diff(time.ticks_ms(), start)
            print("announce: connect failed — " + str(e))
            return False

        await asyncio.sleep_ms(0)
        _set_timeout(s, raw, _IO_SLICE_S)

        # Send: one slice per pass, each bounded by _IO_SLICE_S and separated by
        # an await. A stalled or slow-reading peer ends as a timeout OSError or
        # a zero-byte send, both terminal here, so this loop cannot run away.
        _last_attempt["phase"] = "sending"
        total = len(request)
        sent = 0
        while sent < total:
            await asyncio.sleep_ms(0)
            try:
                n = s.send(request[sent:])
            except Exception as e:
                _last_attempt["phase"] = "send failed"
                print("announce: send failed — " + str(e))
                return False
            if not n:
                _last_attempt["phase"] = "send stalled"
                print("announce: send stalled (" + str(sent) + "/" + str(total) + " bytes)")
                return False
            sent += n

        # Read the header block one bounded slice at a time until the headers
        # end, the peer closes, or _RESP_BUDGET_MS expires: that budget is what
        # bounds the NUMBER of slices, since each recv() has a fresh timeout.
        _last_attempt["phase"] = "waiting for response"
        hdr = b""
        resp_start = time.ticks_ms()
        while b"\r\n\r\n" not in hdr:
            await asyncio.sleep_ms(0)
            if time.ticks_diff(time.ticks_ms(), resp_start) >= _RESP_BUDGET_MS:
                _last_attempt["phase"] = "response budget expired"
                break
            try:
                chunk = s.recv(_RECV_CHUNK)
            except Exception as e:
                # isinstance guard: only a slice timeout continues, so a
                # non-OSError whose args happen to match never loops on.
                if isinstance(e, OSError) and _is_timeout(e):
                    continue
                _last_attempt["phase"] = "recv error"
                print("announce: recv error — " + str(e))
                break
            if chunk is None:
                continue
            if len(chunk) == 0:
                _last_attempt["phase"] = "peer closed"
                break
            hdr += chunk
            if len(hdr) > _HEAD_CAP:
                _last_attempt["phase"] = "header cap reached"
                break

        # Parse the status line only: "HTTP/1.x NNN <reason>".
        _last_attempt["ms"] = time.ticks_diff(time.ticks_ms(), start)
        if hdr:
            line = hdr.split(b"\r\n")[0]
            fields = line.split(b" ")
            if len(fields) >= 2 and line[:4] == b"HTTP":
                try:
                    _last_attempt["status"] = int(fields[1])
                    _last_attempt["phase"] = "status received"
                except:
                    _last_attempt["phase"] = "unparseable status line"
            else:
                _last_attempt["phase"] = "no status line"

        ok = _last_attempt["status"] is not None and 200 <= _last_attempt["status"] < 300
        if ok:
            _last_attempt["phase"] = "ok"
        return ok
    finally:
        # Closed on every path; this cleanup has no await and close() on a
        # socket we own does not block. A wrapped socket owns the raw one, so
        # closing both is harmless.
        for so in (s, raw):
            try:
                if so is not None:
                    so.close()
            except:
                pass


# ---------------------------------------------------------------------------
# RETRY / BACKOFF LOOP — the module's entry point: keep announcing until the hub
# accepts, forever if it must. Every wait is `await asyncio.sleep()` and
# stop_event is checked each second, so shutdown stays responsive and
# app/wifi.py's link-up cancel/respawn keeps working.
# ---------------------------------------------------------------------------


async def announce_then_retry_once(cfg, wlan, io=None, stop_event=None):
    """Announce this unit to the hub. Attempt 1 runs immediately; after a
    failure attempts continue FOREVER on the _RETRY_BACKOFF_S schedule (30, 30,
    60, 120, 300, then 300 s) until one succeeds. True on success, False if the
    unit shuts down first.

    Attempts are cooperative and resolve no names, so regulation and the
    watchdog feed continue through the whole retry sequence. LED stays solid ON
    while unacknowledged, heartbeat once the hub accepts.

    Re-entrant: app/wifi.py spawns a fresh one after cancelling any prior
    announce task, so two tasks never fight over the LED."""
    if io and hasattr(io, "led_solid"):
        io.led_solid(True)

    n = 1
    while True:
        ok = await _post_once_async(cfg, wlan)
        _log_attempt(n, ok)
        if ok:
            if io:
                io.led_flash(cycles=2, period_ms=120)
                io.start_led_heartbeat(period_ms=800, stop_event=stop_event)
            return True

        # wait, but stay cancelable
        wait_s = _RETRY_BACKOFF_S[min(n - 1, len(_RETRY_BACKOFF_S) - 1)]
        remaining = wait_s
        while remaining > 0:
            if stop_event and stop_event.is_set():
                # Never reached the hub — stay solid ON and exit for shutdown.
                return False
            await asyncio.sleep(1)
            remaining -= 1
        n += 1

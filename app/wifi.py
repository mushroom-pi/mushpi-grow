import network, time, uasyncio as asyncio


def _candidates(wifi_cfg):
    """Build ordered list of (ssid, password) from config.
    Primary ssid/password first, then any entries in 'networks' list."""
    candidates = []
    nets = wifi_cfg.get("networks")
    if nets and isinstance(nets, list) and len(nets) > 0:
        for n in nets:
            if isinstance(n, dict) and n.get("ssid"):
                candidates.append((n["ssid"], n.get("password", "")))
    # Primary ssid/password always first (if present)
    primary_ssid = wifi_cfg.get("ssid", "")
    if primary_ssid:
        primary = (primary_ssid, wifi_cfg.get("password", ""))
        # avoid duplicate if already in networks list
        if not candidates or candidates[0] != primary:
            candidates.insert(0, primary)
    # Fallback: if nothing from networks and no primary, empty list
    if not candidates and primary_ssid:
        candidates = [(primary_ssid, wifi_cfg.get("password", ""))]
    return candidates


def connect_wifi(wifi_cfg, hostname=None, io=None, retries=3, retry_gap_ms=5000):
    """Try each candidate network in order; return (wlan, ip) or (wlan, None)."""
    candidates = _candidates(wifi_cfg)

    # No networks configured — skip connection attempts, let caller enter AP mode
    if not candidates:
        wlan = network.WLAN(network.STA_IF)
        wlan.active(False)
        print("wifi: no SSID configured — skipping STA connect")
        return (wlan, None)

    if io:
        io.led_off()

    wlan = network.WLAN(network.STA_IF)
    if hostname:
        try:
            network.hostname(hostname)
        except Exception:
            try:
                wlan.config(hostname=hostname)
            except:
                pass
    wlan.active(True)

    for ssid, password in candidates:
        for attempt in range(retries):
            print("wifi: trying", ssid, "attempt", attempt + 1)
            if wlan.isconnected():
                break
            wlan.connect(ssid, password)
            for _ in range(100):  # ~10s per attempt
                if wlan.isconnected():
                    break
                time.sleep_ms(100)
            if wlan.isconnected():
                break
            # attempt failed
            wlan.disconnect()
            if io:
                io.led_off()
            if attempt < retries - 1:
                time.sleep_ms(retry_gap_ms)

    ip = wlan.ifconfig()[0] if wlan.isconnected() else None

    if ip and io and hasattr(io, "led_on"):
        io.led_on()
    print("WiFi:", "up" if ip else "down", ip)
    return (wlan, ip)


_BACKOFF_MS = [5000, 10000, 30000, 60000]


async def reconnect_once_async(wlan, wifi_cfg, io=None, per_attempt_ms=10000):
    """Try each candidate network once (async). Returns True on success."""
    try:
        wlan.active(True)
    except:
        pass

    for ssid, password in _candidates(wifi_cfg):
        if wlan.isconnected():
            return True
        try:
            wlan.disconnect()
        except:
            pass
        try:
            wlan.connect(ssid, password)
        except:
            print("wifi: connect error for", ssid)
            continue

        # Poll async until connected or timeout
        elapsed = 0
        while elapsed < per_attempt_ms:
            if wlan.isconnected():
                print("wifi: reconnected to", ssid)
                return True
            await asyncio.sleep_ms(100)
            elapsed += 100

        # This candidate failed
        try:
            wlan.disconnect()
        except:
            pass
        print("wifi:", ssid, "failed")

    return False


async def wifi_watchdog_loop(cfg, wlan, io=None, stop_event=None):
    """Monitor WiFi link; reconnect with backoff when it drops."""
    from .state import attach_wlan_info

    poll_interval_ms = 5000

    while not (stop_event and stop_event.is_set()):
        if wlan.isconnected():
            await asyncio.sleep_ms(poll_interval_ms)
            continue

        # --- Disconnected transition ---
        rssi = "?"
        try:
            rssi = str(wlan.status('rssi'))
        except:
            pass
        print("wifi: link down — starting reconnect (RSSI was " + rssi + ")")

        # Link down: bring the unit's own AP up so the grower can still reach
        # the page and read state / change targets during the outage. The
        # control loop keeps regulating (it never touches WiFi). The
        # offline double-blink replaces the heartbeat — start_provisioning_
        # blink() cancels the heartbeat task first, exactly as the previous
        # led_solid(False) force-clear did.
        try:
            ensure_ap_up(cfg)
        except Exception as e:
            print("wifi: AP bring-up failed:", e)
        if io and hasattr(io, "start_provisioning_blink"):
            try:
                io.start_provisioning_blink(stop_event=stop_event)
            except:
                pass

        idx = 0
        while not (stop_event and stop_event.is_set()):
            print("wifi: reconnect attempt", idx + 1)
            ok = await reconnect_once_async(wlan, cfg["wifi"], io)
            if ok:
                attach_wlan_info(wlan, cfg)
                ip = "?"
                try:
                    ip = wlan.ifconfig()[0]
                except:
                    pass
                print("wifi: link up at", ip)
                # Link back: drop the outage AP again (STA is serving the unit
                # now). Idempotent — a no-op if it was never up.
                try:
                    ap_down()
                except Exception as e:
                    print("wifi: AP teardown failed:", e)
                # No announce respawn here. main.py's single boot announce task is
                # still running its backoff loop and will reach the hub on its own;
                # respawning one here is what created the overlapping-task bug.
                # The respawn's OTHER job — restarting the heartbeat that the
                # link-down blink replaced — is done directly:
                # start_led_heartbeat() cancels any existing blink/heartbeat task
                # before creating one, so it cannot double-start (and only the
                # link-down blink can be live here). Cold start on a
                # genuine link-up, from inside the running loop — exactly what
                # the helper documents.
                if io and hasattr(io, "start_led_heartbeat"):
                    try:
                        io.start_led_heartbeat(period_ms=800, stop_event=stop_event)
                    except:
                        pass
                break

            # Backoff
            delay = _BACKOFF_MS[min(idx, len(_BACKOFF_MS) - 1)]
            print("wifi: reconnect failed — retry in", delay // 1000, "s")
            # Sleep in 200ms chunks for responsive shutdown
            remaining = delay
            while remaining > 0:
                if stop_event and stop_event.is_set():
                    return
                await asyncio.sleep_ms(min(200, remaining))
                remaining -= 200
            idx += 1


def ap_setup_ssid(device_name):
    """Generate provisioning SSID: mushpi-provision-XXXX (last 4 hex of AP MAC)."""
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        mac = ap.config('mac')
        ap.active(False)
        hex_str = "".join("%02x" % b for b in mac)
        suffix = hex_str[-4:]
        return "mushpi-provision-" + suffix
    except:
        return "mushpi-provision"


# AP interface currently up for offline operation (None = not up). Module-level
# so ensure_ap_up() can be idempotent: ap_setup_ssid() momentarily toggles the
# AP (active(True) → read MAC → active(False)) to derive the SSID, and re-
# running it against a live interface would flap it — dropping any client
# already attached. The flag is the single source of truth for AP lifecycle.
_ap_if = None


def ensure_ap_up(cfg):
    """Bring the offline (AP) interface up, or return it if already up.

    One code path shared by the boot entry (start_ap_provisioning) and the
    Wi-Fi watchdog's runtime link-down bring-up. Idempotent by the _ap_if
    flag above. Interface config here is synchronous by design and matches
    the established runtime pattern (reconnect_once_async() likewise calls
    wlan.active(True) from inside the loop); the connect polling that follows
    is what stays async."""
    global _ap_if
    if _ap_if is not None:
        return _ap_if
    ssid = ap_setup_ssid(cfg.get("device_name", "pico"))
    ap = network.WLAN(network.AP_IF)
    ap.config(ssid=ssid, security=0)  # open network
    ap.active(True)
    ap.ifconfig(("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.1"))
    _ap_if = ap
    print("AP: up — SSID:", ssid, "IP: 192.168.4.1")
    return ap


def ap_down():
    """Take the offline AP down (STA link recovered). Idempotent — a no-op
    when the AP was never up."""
    global _ap_if
    if _ap_if is None:
        return
    try:
        _ap_if.active(False)
    except:
        pass
    _ap_if = None
    print("AP: down")


def start_ap_provisioning(cfg):
    """Boot entry into the offline (AP) operating mode. Thin wrapper so the
    boot path and the watchdog share one AP-setup code path."""
    return ensure_ap_up(cfg)

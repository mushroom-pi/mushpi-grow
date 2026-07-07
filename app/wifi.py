import network, time

def connect_wifi(wifi_cfg, hostname=None, io=None, retries=3, retry_gap_ms=5000):
    """Try each candidate network in order; return (wlan, ip) or (wlan, None)."""
    # Build candidate list
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


def start_ap_provisioning(cfg):
    """Activate open AP for provisioning. Returns the AP interface."""
    ap = network.WLAN(network.AP_IF)
    ssid = ap_setup_ssid(cfg.get("device_name", "pico"))
    ap.config(ssid=ssid, security=0)  # open network
    ap.active(True)
    ap.ifconfig(("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.1"))
    print("AP: up — SSID:", ssid, "IP: 192.168.4.1")
    return ap

import network, time

def connect_wifi(ssid, password, hostname=None):
    wlan = network.WLAN(network.STA_IF)
    # Set host name
    try:
        if hostname: wlan.config(hostname=hostname)
    except: pass
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        for _ in range(100):          # ~10s
            if wlan.isconnected(): break
            time.sleep_ms(100)
    ip = wlan.ifconfig()[0] if wlan.isconnected() else None
    print("WiFi:", "up" if ip else "down", ip)
    return wlan

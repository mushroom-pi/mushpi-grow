import network, time

def connect_wifi(ssid, password, hostname=None, io=None):
    io.led_off();
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

    if ip and io and hasattr(io, "led_on"): io.led_on()
    print("WiFi:", "up" if ip else "down", ip) 
    return wlan

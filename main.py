import network
import uasyncio as asyncio
import machine
import dht
import time
import ujson
import urequests
from microdot import Microdot, Response

# Load secrets/config
with open("config.json") as f:
    config = ujson.load(f)

# ------------- Configuration -------------
WIFI_SSID = config["wifi"]["ssid"]
WIFI_PASSWORD = config["wifi"]["password"]

# ---- Discovery/registration settings ----
HUB_URL = config["hub_url"]
DEVICE_NAME = config["device_name"]
API_PORT = 5000                           # the port your Pico serves on
ANNOUNCE_INTERVAL_S = 60                  # heartbeat period

# GPIO pin assignments (Pico 2 W)
PIN_DHT = 4
PIN_FAN = 16
PIN_HUM = 17
PIN_HEATER = 18

# Relay/MOSFET polarity: set to 1 if "1 = ON", 0 if "0 = ON" (active-low boards)
ACTIVE_HIGH = True  # set False if your relay board is active-low

CONTROL_PERIOD_S = 5
HYST_HUM = 5          # %RH deadband
HYST_TEMP = 1         # °C deadband

# Probe behavior
DEFAULT_PROBE_PULSE_MS = 300
ALLOW_HEATER_IN_PROBE_DEFAULT = False

# Default setpoints
setpoints = {"temperature": 25, "humidity": 60}

# ------------- Hardware setup -------------


def on_value():
    return 1 if ACTIVE_HIGH else 0


def off_value():
    return 0 if ACTIVE_HIGH else 1


# Outputs
relay_fan = machine.Pin(PIN_FAN, machine.Pin.OUT, value=off_value())
relay_humidifier = machine.Pin(PIN_HUM, machine.Pin.OUT, value=off_value())
relay_heater = machine.Pin(PIN_HEATER, machine.Pin.OUT, value=off_value())

# Sensor
dht_sensor = dht.DHT11(machine.Pin(PIN_DHT))

# State
status = {
    "temperature": None,
    "humidity": None,
    "fan": False,
    "humidifier": False,
    "heater": False,
    "last_sensor_ok_at": None,
    "last_sensor_error": None,
}

boot_ts = time.time()

# ------------- Wi-Fi -------------


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    # One of these two should work but, unfortunately, it doesn't
    wlan.config(hostname=DEVICE_NAME)
    network.hostname(DEVICE_NAME)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        # Quick wait loop
        for _ in range(60):  # ~6s total (100ms steps)
            if wlan.isconnected():
                break
            time.sleep_ms(100)
    ip = wlan.ifconfig()[0]
    print(f'Connected on {ip}')
    print(f"Hostname: {network.hostname()}")
    return wlan


wlan = connect_wifi()

# ------------- Helpers -------------


def gpio_write(pin_obj, on: bool):
    try:
        pin_obj.value(on_value() if on else off_value())
        return True
    except Exception as e:
        return False


def gpio_is_on(pin_obj):
    try:
        return (pin_obj.value() == on_value())
    except:
        return False


def plausible_read(temp, hum):
    ok = True
    if temp is None or hum is None:
        return False
    # Basic plausibility window (customize for your environment)
    if not (-10 <= temp <= 60):
        ok = False
    if not (0 <= hum <= 100):
        ok = False
    return ok

# ------------- Control loop -------------


async def control_loop():
    global wlan
    while True:
        # Reconnect Wi-Fi if needed
        if (not wlan.isconnected()):
            wlan = connect_wifi()

        # Read sensor
        try:
            dht_sensor.measure()
            temp = dht_sensor.temperature()
            hum = dht_sensor.humidity()

            status["temperature"] = temp
            status["humidity"] = hum

            if plausible_read(temp, hum):
                status["last_sensor_ok_at"] = time.time()
                status["last_sensor_error"] = None
            else:
                status["last_sensor_error"] = "implausible_reading"

            # --- Humidity control ---
            if hum is not None:
                if hum < setpoints["humidity"] - HYST_HUM:
                    gpio_write(relay_humidifier, True)
                    status["humidifier"] = True
                    gpio_write(relay_fan, False)
                    status["fan"] = False
                elif hum > setpoints["humidity"] + HYST_HUM:
                    gpio_write(relay_fan, True)
                    status["fan"] = True
                    gpio_write(relay_humidifier, False)
                    status["humidifier"] = False
                else:
                    # within band
                    gpio_write(relay_fan, False)
                    status["fan"] = False
                    gpio_write(relay_humidifier, False)
                    status["humidifier"] = False

            # --- Temperature control ---
            if temp is not None:
                if temp < setpoints["temperature"] - HYST_TEMP:
                    gpio_write(relay_heater, True)
                    status["heater"] = True
                elif temp > setpoints["temperature"] + HYST_TEMP:
                    gpio_write(relay_heater, False)
                    status["heater"] = False
                # if within band, keep previous heater state (or turn off to be conservative)
                else:
                    gpio_write(relay_heater, False)
                    status["heater"] = False

        except Exception as e:
            status["last_sensor_error"] = str(e)

        await asyncio.sleep(CONTROL_PERIOD_S)

# ------- DISCOVERY MODE --------

def _get_ip():
    try:
        return wlan.ifconfig()[0]
    except:
        return None

def _get_mac():
    try:
        mac = network.WLAN().config('mac')  # bytes
        return ":".join("%02x" % b for b in mac)
    except:
        return None

def announce_payload():
    return {
        "name": DEVICE_NAME,
        "host": _get_ip(),
        "port": API_PORT,
    }

def announce_once():
    data = announce_payload()
    if not data["host"]:
        print("announce: no IP yet")
        return False
    try:
        r = urequests.post(HUB_URL, json=data)
        # reading content ensures socket closes cleanly
        _ = r.text
        r.close()
        print("announce: ok", data["host"])
        return True
    except Exception as e:
        print("announce: fail:", e)
        return False

async def announce_loop():
    """Periodic heartbeat with simple backoff."""
    # small delay so Wi-Fi stabilizes
    await asyncio.sleep(2)
    backoff = ANNOUNCE_INTERVAL_S
    while True:
        ok = announce_once()
        # if it failed, retry sooner; if ok, wait normal period
        await asyncio.sleep(10 if not ok else ANNOUNCE_INTERVAL_S)

# ------------- API -------------

app = Microdot()
Response.default_content_type = 'application/json'


@app.get('/status')
def get_status(request):
    # Current device states + readings
    return status


@app.get('/setpoints')
def get_setpoints(request):
    return setpoints


@app.post('/setpoints')
async def post_setpoints(request):
    try:
        data = await request.json()
        if "temperature" in data:
            setpoints["temperature"] = int(data["temperature"])
        if "humidity" in data:
            setpoints["humidity"] = int(data["humidity"])
        return {"ok": True, "setpoints": setpoints}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 400


@app.get('/health')
def get_health(request):
    now = time.time()
    wifi = {
        "connected": wlan.isconnected(),
        "ifconfig": wlan.ifconfig() if wlan.isconnected() else None
    }

    # Check GPIO channels are writable and reflect expected logic level
    gpio_report = {
        "fan_gpio_ok": isinstance(relay_fan, machine.Pin),
        "humidifier_gpio_ok": isinstance(relay_humidifier, machine.Pin),
        "heater_gpio_ok": isinstance(relay_heater, machine.Pin),
        "active_high": ACTIVE_HIGH,
    }

    # Sensor freshness
    last_ok = status["last_sensor_ok_at"]
    age = (now - last_ok) if last_ok else None
    sensor_ok = plausible_read(status["temperature"], status["humidity"])

    return {
        "uptime_s": int(now - boot_ts),
        "wifi": wifi,
        "sensor": {
            "ok": sensor_ok,
            "temp": status["temperature"],
            "humidity": status["humidity"],
            "last_ok_age_s": int(age) if age is not None else None,
            "last_error": status["last_sensor_error"],
        },
        "gpio": gpio_report,
    }


@app.post('/probe')
async def post_probe(request):
    """
    Optional wiring check. Requires JSON: {"allow_toggle": true}
    Optional flags: {"include_heater": true, "pulse_ms": 300}
    Heats/cycles loads briefly; use with caution.
    """
    try:
        data = await request.json()
    except:
        data = {}

    if not data.get("allow_toggle", False):
        return {"ok": False, "error": "Set allow_toggle=true to run probe."}, 400

    include_heater = bool(
        data.get("include_heater", ALLOW_HEATER_IN_PROBE_DEFAULT))
    pulse_ms = int(data.get("pulse_ms", DEFAULT_PROBE_PULSE_MS))
    pulse_ms = max(100, min(pulse_ms, 2000))  # clamp 0.1–2.0s

    report = {}

    # Helper to pulse one output
    async def pulse(name, pin):
        before = gpio_is_on(pin)
        gpio_write(pin, True)
        await asyncio.sleep_ms(pulse_ms)
        gpio_write(pin, False)
        after = gpio_is_on(pin)
        return {"before": before, "after": after, "pulse_ms": pulse_ms}

    # Fan
    report["fan"] = await pulse("fan", relay_fan)

    # Humidifier
    report["humidifier"] = await pulse("humidifier", relay_humidifier)

    # Heater (optional)
    if include_heater:
        report["heater"] = await pulse("heater", relay_heater)
    else:
        report["heater"] = {"skipped": True}

    return {"ok": True, "report": report}

# ------------- Main -------------


async def main():
    asyncio.create_task(control_loop())
    asyncio.create_task(announce_loop())
    await app.start_server(host='0.0.0.0', port=5000)

asyncio.run(main())

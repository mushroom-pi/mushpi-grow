import uasyncio as asyncio
import utime as time

from .state import status, setpoints

# Per-relay "time switched ON" in ticks_ms; None = not currently ON-or-in-runtime-window
_on_since = {"humidifier": None, "fan": None, "heater": None}

control_enabled_event = asyncio.Event()
control_enabled_event.set()

def set_control_enabled(enabled: bool):
    if enabled:
        control_enabled_event.set()
    else:
        control_enabled_event.clear()

def is_control_enabled():
    return control_enabled_event.is_set()

def _can_turn_off(relay, min_runtime_ms):
    since = _on_since[relay]
    if since is None:
        return True
    elapsed_ms = time.ticks_diff(time.ticks_ms(), since)
    return elapsed_ms >= min_runtime_ms

def _turn_on(io, relay):
    if relay == "humidifier": io.hum_on()
    elif relay == "fan": io.fan_on()
    elif relay == "heater": io.heat_on()
    _on_since[relay] = time.ticks_ms()

def _turn_off(io, relay, min_runtime_ms):
    if not _can_turn_off(relay, min_runtime_ms):
        return
    if relay == "humidifier": io.hum_off()
    elif relay == "fan": io.fan_off()
    elif relay == "heater": io.heat_off()
    _on_since[relay] = None

def reset_on_since():
    for relay in _on_since:
        _on_since[relay] = None

def mark_relay_off(relay):
    _on_since[relay] = None

def mark_relay_on(relay):
    _on_since[relay] = time.ticks_ms()

def evaluate_hysteresis(cfg, io):
    """Actuation step with asymmetric deadband and minimum runtime gate.
    Humidifier ON at h <= target - humidity_deadband (low-side).
    Fan ON at h > target + hyst_hum (high-side).
    Heater ON at t <= target - temperature_deadband.
    Relays stay ON for at least min_runtime seconds before they can turn off.
    Does NOT check control_enabled; the caller is responsible for gating.
    Reads status/setpoints live (no snapshots)."""
    Hh = cfg["control"]["hyst_hum"]
    Hd = cfg["control"]["humidity_deadband"]
    Td = cfg["control"]["temperature_deadband"]
    Mr = cfg["control"]["min_runtime"] * 1000

    t = status["temperature"]
    h = status["humidity"]
    ht = setpoints["humidity"]
    tt = setpoints["temperature"]

    # Humidity control — humidifier and fan are mutually exclusive
    if h is not None:
        if h <= ht - Hd:
            # Below low-side deadband: run humidifier to push UP
            _turn_on(io, "humidifier")
            _turn_off(io, "fan", Mr)
        elif h > ht + Hh:
            # Above high-side deadband: run fan to push DOWN
            _turn_on(io, "fan")
            _turn_off(io, "humidifier", Mr)
        else:
            # Inside deadband — neither runs (gated by min_runtime)
            _turn_off(io, "humidifier", Mr)
            _turn_off(io, "fan", Mr)

    # Temperature — heater (adds heat), no cooler counterpart
    if t is not None:
        if t <= tt - Td:
            _turn_on(io, "heater")
        else:
            _turn_off(io, "heater", Mr)

async def control_loop(cfg, wlan, io, sensor, stop_event=None, wdt=None):
    P = cfg["control"]["period_s"]
    relays_latched = False  # ensures relays_off() called only once when disabled

    while not (stop_event and stop_event.is_set()):
        # WiFi monitoring/reconnect handled by wifi_watchdog_loop

        # Always sample the sensor so API/server polling stays fresh
        # regardless of whether control is enabled (observability vs actuation).
        try:
            sensor.read()
        except:
            pass

        if not control_enabled_event.is_set():
            # Control disabled: turn off relays once, keep LED heartbeat alive.
            if not relays_latched:
                try:
                    io.relays_off()
                    reset_on_since()
                    relays_latched = True
                except:
                    pass
        else:
            # Control enabled: reset latch and run hysteresis actuation.
            relays_latched = False
            evaluate_hysteresis(cfg, io)

        # Sleep in small chunks for responsive shutdown
        chunks = (P * 1000) // 200
        for _ in range(chunks):
            if stop_event and stop_event.is_set():
                return
            if wdt:
                wdt.feed()
            await asyncio.sleep_ms(200)


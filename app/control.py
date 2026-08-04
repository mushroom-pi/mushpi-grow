import uasyncio as asyncio

from .state import status, setpoints

control_enabled_event = asyncio.Event()
control_enabled_event.set()

def set_control_enabled(enabled: bool):
    if enabled:
        control_enabled_event.set()
    else:
        control_enabled_event.clear()

def is_control_enabled():
    return control_enabled_event.is_set()

def evaluate_hysteresis(cfg, io):
    """Pure actuation step — applies current setpoints against cached sensor
    readings to drive relays. Does NOT check control_enabled; the caller is
    responsible for gating. Reads status/setpoints live (no snapshots).
    Purely threshold-based — no state tracking needed."""
    Hh = cfg["control"]["hyst_hum"]
    Ht = cfg["control"]["hyst_temp"]
    t = status["temperature"]
    h = status["humidity"]
    ht = setpoints["humidity"]
    tt = setpoints["temperature"]

    # Humidity
    if h is not None:
        if h <= ht:
            # Below or at target: run humidifier to push UP
            io.hum_on()
            io.fan_off()      # safety: mutually exclusive
        elif h > ht + Hh:
            # Significantly above target: run fan to push DOWN
            io.fan_on()
            io.hum_off()      # safety: mutually exclusive
        else:
            # ht < h <= ht + Hh: deadband — neither runs
            io.hum_off()
            io.fan_off()

    # Temperature — heater (adds heat), no cooler counterpart
    if t is not None:
        if t <= tt:
            io.heat_on()
        else:
            io.heat_off()

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


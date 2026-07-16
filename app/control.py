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

async def control_loop(cfg, wlan, io, sensor, stop_event=None):
    P = cfg["control"]["period_s"]
    Hh = cfg["control"]["hyst_hum"]
    Ht = cfg["control"]["hyst_temp"]
    relays_latched = False  # ensures relays_off() called only once when disabled

    while not (stop_event and stop_event.is_set()):
        if not wlan.isconnected():
            # Try to nudge reconnect (optional)
            pass

        # Always sample the sensor so API/server polling stays fresh
        # regardless of whether control is enabled (observability vs actuation).
        try:
            sensor.read()
        except:
            pass

        t, h = status["temperature"], status["humidity"]

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

            # Humidity control
            if h is not None:
                if h < setpoints["humidity"] - Hh:
                    io.hum_on()
                    io.fan_off()
                elif h > setpoints["humidity"] + Hh:
                    io.fan_on()
                    io.hum_off()
                else:
                    io.hum_off()
                    io.fan_off()

            # Temperature control
            if t is not None:
                if t < setpoints["temperature"] - Ht:
                    io.heat_on()
                elif t > setpoints["temperature"] + Ht:
                    io.heat_off()
                else:
                    io.heat_off()

        # Sleep in small chunks for responsive shutdown
        chunks = (P * 1000) // 200
        for _ in range(chunks):
            if stop_event and stop_event.is_set():
                return
            await asyncio.sleep_ms(200)


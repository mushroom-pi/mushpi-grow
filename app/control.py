import uasyncio as asyncio

from .state import status, setpoints, control_enabled

control_enabled_event = asyncio.Event()
control_enabled_event.set()

def set_control_enabled(enabled: bool):
    global control_enabled
    control_enabled = bool(enabled)
    if control_enabled:
        control_enabled_event.set()
    else:
        control_enabled_event.clear()

def is_control_enabled() -> bool:
    return control_enabled

async def control_loop(cfg, wlan, io, sensor, stop_event=None):
    P = cfg["control"]["period_s"]
    Hh = cfg["control"]["hyst_hum"]
    Ht = cfg["control"]["hyst_temp"]
    all_off = False # This variable allows turning all the devices off only once when the control loop is disabled

    while not (stop_event and stop_event.is_set()):
        if not wlan.isconnected():
            # Try to nudge reconnect (optional)
            pass
      
        if not control_enabled_event.is_set():
            if not all_off:
                try: io.all_off(); all_off = True
                except: pass

            # Sleep in small chunks so shutdown is responsive
            for _ in range(10):
                if stop_event and stop_event.is_set():
                    return
                if control_enabled_event.is_set():
                    break
                await asyncio.sleep_ms(100)
            continue

        all_off = False
        sensor.read()
        t, h = status["temperature"], status["humidity"]

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

        await asyncio.sleep(P)


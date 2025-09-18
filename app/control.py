import uasyncio as asyncio
from .state import status, setpoints

async def control_loop(cfg, wlan, io, sensor):
    P = cfg["control"]["period_s"]
    Hh = cfg["control"]["hyst_hum"]
    Ht = cfg["control"]["hyst_temp"]

    while True:
        if not wlan.isconnected():
            # Try to nudge reconnect (optional)
            pass

        sensor.read()
        t, h = status["temperature"], status["humidity"]

        # Humidity control
        if h is not None:
            if h < setpoints["humidity"] - Hh:
                io.write(io.hum, True);  status["humidifier"] = True
                io.write(io.fan, False); status["fan"] = False
            elif h > setpoints["humidity"] + Hh:
                io.write(io.fan, True);  status["fan"] = True
                io.write(io.hum, False); status["humidifier"] = False
            else:
                io.write(io.fan, False); status["fan"] = False
                io.write(io.hum, False); status["humidifier"] = False

        # Temperature control
        if t is not None:
            if t < setpoints["temperature"] - Ht:
                io.write(io.heat, True);  status["heater"] = True
            elif t > setpoints["temperature"] + Ht:
                io.write(io.heat, False); status["heater"] = False
            else:
                io.write(io.heat, False); status["heater"] = False

        await asyncio.sleep(P)


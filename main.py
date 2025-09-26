import time, uasyncio as asyncio

from app.config_loader import load_config
from app.state import boot_ts
from app.wifi import connect_wifi
from app.hw import IO
from app.sensor import DHTReader
from app.control import control_loop
from app.announce import announce_loop
from app.api import start_server
from app.shutdown import graceful_shutdown

boot_ts = time.time()

# Hardware & sensor
io = IO() ## This will turn the LED ON
sensor = DHTReader()

cfg = load_config()

# Bring up Wi-Fi
wlan = connect_wifi(cfg["wifi"]["ssid"], cfg["wifi"]["password"], cfg["device_name"], io) ## This will start by turning the ledd off

async def main():
    asyncio.create_task(control_loop(cfg, wlan, io, sensor))
    asyncio.create_task(announce_loop(cfg, wlan, io))
    await start_server(cfg, wlan, io, sensor)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("KeyboardInterrupt — shutting down...")
except Exception as e:
    import sys
    sys.print_exception(e)
finally:
    # Run an async cleanup in a fresh event loop
    asyncio.run(graceful_shutdown(wlan, io, sensor))
    print("Shutdown complete.")

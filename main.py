import time, uasyncio as asyncio

from app.config_loader import load_config
from app.state import boot_ts
from app.wifi import connect_wifi
from app.hw import IO
from app.sensor import DHTReader
from app.control import control_loop
from app.announce import announce_loop
from app.api import start_server

cfg = load_config()
boot_ts = time.time()

# Bring up Wi-Fi
wlan = connect_wifi(cfg["wifi"]["ssid"], cfg["wifi"]["password"], cfg["device_name"])

# Hardware & sensor
io = IO(cfg)
sensor = DHTReader(io.dht_pin)

async def main():
    asyncio.create_task(control_loop(cfg, wlan, io, sensor))
    # asyncio.create_task(announce_loop(cfg, wlan))
    await start_server(cfg, wlan, io, sensor)

asyncio.run(main())

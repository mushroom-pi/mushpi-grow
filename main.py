import uasyncio as asyncio

from app.config_loader import load_config
import app.state as state
from app.wifi import connect_wifi
from app.hw import IO
from app.sensor import DHTReader
from app.control import control_loop
from app.announce import announce_then_retry_once
from app.api import start_server
from app.shutdown import graceful_shutdown, stop_event
from app.metrics import start_metrics

# Hardware & sensor
io = IO()  # This will turn the LED ON
sensor = DHTReader()

# General variables
cfg = load_config()
state.init_system_info(cfg)
state.check_mdns_firmware(cfg["device_name"])

# Bring up Wi-Fi
wlan = connect_wifi(cfg["wifi"]["ssid"], cfg["wifi"]["password"],
                    # This will start by turning the ledd off
                    cfg["device_name"], io)
state.attach_wlan_info(wlan, cfg)


async def main():
    start_metrics()
    asyncio.create_task(announce_then_retry_once(
        cfg, wlan, io, delay_s=60, timeout_s=2, stop_event=stop_event))
    asyncio.create_task(control_loop(
        cfg, wlan, io, sensor, stop_event=stop_event))
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

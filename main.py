import uasyncio as asyncio

from machine import Pin, WDT

from app.config_loader import load_config
import app.state as state
from app.wifi import connect_wifi, start_ap_provisioning, wifi_watchdog_loop
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
cfg, load_errors = load_config()
from app.config_validator import validate_config
errors = load_errors + validate_config(cfg)
if errors:
    for err in errors:
        print(err)
    io.all_off()
    io.error_blink_blocking()
from app.uptime import init_uptime
init_uptime()
state.init_system_info(cfg)
state.check_mdns_firmware(cfg["device_name"])

# Force-provision check: GP0 held LOW → skip STA, go straight to AP
_force_provision = False
try:
    _gp0 = Pin(0, Pin.IN, Pin.PULL_UP)
    if _gp0.value() == 0:
        _force_provision = True
        print("force-provision: GP0 is LOW")
except Exception as e:
    print("GP0 check error:", e)

# No Wi-Fi configured — go straight to AP provisioning
if not cfg["wifi"].get("ssid"):
    print("No Wi-Fi configured — entering provisioning mode")
    _force_provision = True

# Bring up Wi-Fi
wlan, ip = connect_wifi(cfg["wifi"],
                        hostname=cfg["device_name"], io=io,
                        retries=3, retry_gap_ms=5000)

_ap_mode = _force_provision or (ip is None)

if _ap_mode:
    ap = start_ap_provisioning(cfg)
    io.start_provisioning_blink(stop_event=stop_event)
    state.attach_wlan_info(ap, cfg)
else:
    ap = None
    state.attach_wlan_info(wlan, cfg)


async def main():
    start_metrics()
    if _ap_mode:
        # AP provisioning mode — no announce, no control loop
        # Relays stay in boot-safe OFF state
        await start_server(cfg, ap, io, sensor, mode="ap")
    else:
        wdt = WDT(timeout=8000)
        from app.reboot_scheduler import reboot_scheduler_loop
        asyncio.create_task(reboot_scheduler_loop(
            cfg, wlan, io, sensor, stop_event=stop_event))
        asyncio.create_task(announce_then_retry_once(
            cfg, wlan, io, delay_s=60, timeout_s=2, stop_event=stop_event))
        asyncio.create_task(control_loop(
            cfg, wlan, io, sensor, stop_event=stop_event, wdt=wdt))
        asyncio.create_task(wifi_watchdog_loop(
            cfg, wlan, io, stop_event=stop_event))
        await start_server(cfg, wlan, io, sensor, mode="sta")

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("KeyboardInterrupt — shutting down...")
except Exception as e:
    import sys
    sys.print_exception(e)
finally:
    # Shut down whichever interface is active
    _active_wlan = ap if _ap_mode else wlan
    asyncio.run(graceful_shutdown(_active_wlan, io, sensor))
    print("Shutdown complete.")

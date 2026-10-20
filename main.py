import uasyncio as asyncio

from machine import Pin, WDT

from app.config_loader import load_config
import app.state as state
from app.wifi import connect_wifi, start_ap_provisioning, wifi_watchdog_loop
from app.hw import IO
from app.sensor import DHTReader
from app.control import control_loop
from app.announce import announce_then_retry_once, presolve_boot_hub
from app.api import start_server
from app.shutdown import graceful_shutdown, stop_event
from app.metrics import start_metrics
from app import runtime_state

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

# Apply persisted GPIO mapping / polarity over boot-time defaults.
# io & sensor were constructed above on defaults; re-init on cfg pins.
from app.state import devices
_pins = cfg.get("pins", {})
if isinstance(_pins, dict):
    for _k in ("dht", "humidifier", "fan", "heater"):
        if _k in _pins:
            devices["pins"][_k] = int(_pins[_k])
if "active_high" in cfg:
    devices["active_high"] = bool(cfg["active_high"])
sensor.__init__()
io.__init__()

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

# No Wi-Fi configured — go straight to the offline (AP) operating mode
if not cfg["wifi"].get("ssid"):
    print("No Wi-Fi configured — entering offline (AP) mode")
    _force_provision = True

# Bring up Wi-Fi
wlan, ip = connect_wifi(cfg["wifi"],
                        hostname=cfg["device_name"], io=io,
                        retries=3, retry_gap_ms=5000)

_ap_mode = _force_provision or (ip is None)

if _ap_mode:
    # Offline (AP) operating mode: no network to join, so the unit brings up
    # its own open AP and regulates against its held targets while serving the
    # grower directly (see REFERENCE §Offline (AP) operating mode).
    ap = start_ap_provisioning(cfg)
    io.start_provisioning_blink(stop_event=stop_event)
    state.attach_wlan_info(ap, cfg)
else:
    ap = None
    state.attach_wlan_info(wlan, cfg)
    # Boot hub name resolution: the only getaddrinfo() the firmware ever runs.
    # It lives here, at module scope on the sanctioned blocking boot path (the
    # same slot connect_wifi() above already occupies), because getaddrinfo has
    # no timeout of its own and the WDT is constructed inside main() below:
    # resolve first, arm second, and no name lookup can ever fall inside the
    # armed watchdog window. Best-effort — it prints, never raises.
    presolve_boot_hub(cfg, wlan)

# Rehydrate grower-chosen state (targets, loop flag, and — when the loop is off
# — hand-set fan/humidifier positions) from runtime.json. Boot path at module
# scope, before asyncio.run() below: it must run before control_loop() is ever
# created so the primed relay latch is seen. Blocking flash reads are sanctioned
# only in this slot (nothing scheduled yet, WDT not yet armed).
runtime_state.restore(cfg, io)


async def main():
    start_metrics()
    # Arm the hardware watchdog exactly once, here: every blocking boot
    # operation has already run at module scope above (connect_wifi,
    # presolve_boot_hub, AP bring-up, runtime_state.restore). Once armed the
    # rp2 watchdog cannot be stopped, so nothing may block between this point
    # and control_loop()'s first feed in either branch below.
    wdt = WDT(timeout=8000)
    if _ap_mode:
        # Offline (AP) operating mode — no hub to answer, so the unit regulates
        # on its own: control loop runs with the stale-sensor safety cut. The
        # only additions STA makes — announce, reboot scheduler, Wi-Fi watchdog
        # — stay off: there is no network to speak of.
        asyncio.create_task(control_loop(
            cfg, ap, io, sensor, stop_event=stop_event, wdt=wdt,
            sensor_fault_cut=True))
        await start_server(cfg, ap, io, sensor, mode="ap")
    else:
        from app.reboot_scheduler import reboot_scheduler_loop
        asyncio.create_task(reboot_scheduler_loop(
            cfg, wlan, io, sensor, stop_event=stop_event))
        asyncio.create_task(announce_then_retry_once(
            cfg, wlan, io, stop_event=stop_event))
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

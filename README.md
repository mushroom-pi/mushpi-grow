# 🍄 Mushroom Pi 🍓 - Pico Growing Unit Firmware

![MicroPython](https://img.shields.io/badge/micropython-%232B2728.svg?style=for-the-badge&logo=micropython&logoColor=white)![Raspberry Pi](https://img.shields.io/badge/-Raspberry_Pi-C51A4A?style=for-the-badge&logo=Raspberry-Pi)![Git](https://img.shields.io/badge/git-%23F05033.svg?style=for-the-badge&logo=git&logoColor=white)

A **MicroPython** repo for [**Raspberry Pi Pico 2 W**](https://www.raspberrypi.com/products/raspberry-pi-pico-2/) to:

1. Control humidity and temperature conditions in a mushroom growing unit.
2. Serve the data via a REST API.

## Required libraries

This repo requires using some extra libraries.

### Using MicroPython's package management

As explained [in Micropython's documentation](https://docs.micropython.org/en/latest/reference/packages.html) packages can be added with `mip`:

```python
  >>> import mip
  >>> mip.install(urequests)
  >>> mip.install(microdot)
```

### Adding manually

Extra packages can be added as single files in the `lib/` folder at the root of the package on execution.

- `urequests.py`: download from [Github](https://github.com/lucien2k/wipy-urllib/blob/master/urequests.py).
- `microdot.py`: download from [Github](https://github.com/miguelgrinberg/microdot/blob/main/src/microdot/microdot.py).

## How to use

### Firmware requirements

For `<device_name>.local` mDNS resolution to work (so you can reach the unit at `http://<device_name>.local:5000/ping` from the same LAN without knowing its IP), the Pico must run **MicroPython v1.26.0 or newer**.

- **v1.25.0 stable** (2025-04-15) contains the mDNS fix ([PR #17057](https://github.com/micropython/micropython/pull/17057)), but **pre-release/preview builds of v1.25.0 do NOT** (they were built before the fix landed).
- **v1.26.0** is the recommended minimum and the version confirmed working on this project.
- **AP-mode mDNS is unsupported** ([#10957](https://github.com/micropython/micropython/issues/10957)) — only STA mode works.
- **mDNS is link-local**: it only resolves on the same subnet. If the hub is on a different VLAN/subnet, you need an mDNS reflector on the router to cross it.

If the firmware is too old, the boot sequence prints a warning referring you here. The DHCP hostname and the REST API on the unit's IP still work regardless.

**Download v1.26.0 firmware**: [RPI_PICO2_W-20250809-v1.26.0.uf2](https://micropython.org/resources/firmware/RPI_PICO2_W-20250809-v1.26.0.uf2)  
**All releases**: https://micropython.org/download/RPI_PICO2_W/

### LED indicator

This software allows minimal communication through the Pico's integrated LED. When the software starts executing, the LED comes on briefly at initialisation, goes off while Wi-Fi is connecting, and comes on solid once the link is up; after that, the possible LED statuses are:

| LED Pattern | Meaning |
|-------------|---------|
| OFF | Wi-Fi lost or not connected — also the runtime link-down state: the LED is cleared when the link drops and stays off while reconnecting |
| SOLID | Wi-Fi connected, hub announcement in progress or failed — a unit that reaches Wi-Fi but never the hub stays lit |
| HEARTBEAT (even blink) | Normal operation |
| CONFIG ERROR (3 fast blinks, 1s pause) | config.json is malformed or has invalid values — check serial output for details |
| SLOW DOUBLE-BLINK (200/200/200/800ms) | AP provisioning mode — awaiting Wi-Fi credentials |

### Provisioning (First-Time Setup or Wi-Fi Recovery)

On first boot (no Wi-Fi configured) or after a Wi-Fi connection failure, the Pico enters **AP provisioning mode**:

1. The LED starts a **slow double-blink** pattern (200ms on / 200ms off / 200ms on / 800ms off).
2. The Pico creates its own open Wi-Fi network: **`mushpi-provision-XXXX`** (where XXXX is the last 4 hex digits of the AP MAC address — no password).
3. Connect to that network from any phone, laptop, or tablet.
4. Open **`http://192.168.4.1:5000`** in a browser.
5. Enter your Wi-Fi network name (SSID) and password in the form.
6. The Pico saves the credentials to `config.json`, reboots, and connects to your network.

**Force-provision**: Hold **GP0 to GND** during boot to force provisioning mode, even if Wi-Fi credentials are already configured. This is useful for changing networks or recovering from bad credentials.

### WiFi Auto-Reconnection

If WiFi drops for any reason (router restart, signal interference, etc.), the Pico handles it automatically without human intervention:

1. **Detection**: The onboard watchdog monitors the WiFi link every 5 seconds. Within ~5s of a disconnection, the LED turns **OFF** and the Pico prints a diagnostic message to the serial console.
2. **Reconnection attempts**: It tries to reconnect immediately, retrying with increasing delays: **5 seconds → 10 seconds → 30 seconds → 60 seconds** (stays at 60s until the link returns). All of the configured WiFi networks (primary SSID + any fallback `networks` in `config.json`) are tried in order on each attempt.
3. **Recovery**: When reconnection succeeds, the LED resumes its **heartbeat** pattern, the Pico announces itself to the server again (so the server knows the new IP), and normal polling resumes.
4. **Control continues**: Throughout the entire outage, the grow tent regulation (humidity/temperature control, relay actuation) keeps running independently. The Pico does **not** reboot on WiFi loss — only the connection is affected.

**What you'll notice**: If your server dashboard shows a unit as "unreachable" but the relays are still clicking and the sensor values look normal, the Pico is fine — wait up to 60 seconds and it should reconnect on its own.

### Watchdog Timer (Auto-Recovery from Freezes)

The Pico has a hardware watchdog timer that acts as a dead man's switch: if the control loop freezes or hangs for more than **8 seconds**, the Pico automatically hard-reboots. After the reboot, it reconnects to WiFi, re-announces to the server, and resumes normal operation — all without human intervention.

This covers rare but critical failures like memory corruption, unhandled exceptions, or an infinite loop. Combined with WiFi auto-reconnection, the system is designed to self-heal from most runtime failures.

### Daily Reboot (Memory Hygiene)

Embedded systems running 24/7 accumulate memory fragmentation and subtle resource leaks over days or weeks. The Pico can be configured to perform a daily soft-reboot at a quiet hour to keep things fresh:

1. **Configuration** — add a `reboot` section to `config.json`:
   ```json
   "reboot": { "enabled": true, "hour": 4, "minute": 0 }
   ```
   (Disabled by default. Also accepts `ntp_host` and `ntp_tz_offset_hours` for timezone adjustment.)

2. **How it works** — After WiFi connects, the Pico syncs its clock via NTP. Every 60 seconds it checks the current time. When the configured hour and minute match, it gracefully shuts down (turns off all relays, disconnects WiFi), then performs a `machine.soft_reset()`. The reboot takes ~5–10 seconds from shutdown to full recovery.

3. **Cumulative uptime** — Unlike power-cycles or watchdog reboots which reset the uptime counter, scheduled reboots **preserve** cumulative uptime in a `uptime.json` file on flash. The dashboard's `uptime.total_s` continues counting across scheduled reboots, so you won't see alarming "just restarted" numbers every morning.

4. **Guards** — Won't reboot twice in the same day, won't reboot before NTP syncs (no clock = no scheduled reboot), and has a 5-minute boot grace period to avoid immediate re-reboot.

The `GET /health` and `GET /system` endpoints report `uptime.reboot_reason` so you can tell at a glance whether the last restart was scheduled, a watchdog recovery, or an unexpected power cycle.

### Manual Reboot (API)

You can trigger a reboot at any time via the REST API:

```bash
# Soft reboot — preserves cumulative uptime
curl -X POST http://<pico-ip>:5000/reboot -d '{"type":"soft"}'

# Hard reboot — full power-cycle, resets uptime
curl -X POST http://<pico-ip>:5000/reboot -d '{"type":"hard"}'
```

The response `{"message":"Reboot initiated","type":"soft"}` returns immediately; the Pico reboots ~2 seconds later. The server also proxies this at `PUT /pico-units/:id/reboot`.

## License

This project is licensed under the **GNU General Public License v3.0** (GPL-3.0).

Copyright (c) 2026 [Adriana Martín de Aguilera](https://www.amda.dev)

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 3 as published by the Free Software Foundation.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

The full license text is in [`LICENSE`](LICENSE).

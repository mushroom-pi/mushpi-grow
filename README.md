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
  >>> mip.install(microdot)
```

### Adding manually

Extra packages can be added as single files in the `lib/` folder at the root of the package on execution.

- `microdot.py`: download from [Github](https://github.com/miguelgrinberg/microdot/blob/main/src/microdot/microdot.py).

Hub announcements need **no** HTTP client library: `app/announce.py` speaks the standard `usocket` stack directly, with every timeout kept as a firmware-internal constant. An `urequests.py` left over in `lib/` from an earlier flash is therefore harmless — the firmware does not import it.

## How to use

### The three ways a unit runs

A unit regulates its climate the same way in every configuration — the only difference is where you look at it and what you get on top. There are three:

1. **No Wi-Fi at all.** The unit keeps regulating against the targets it holds. Its readings and settings are reachable by joining the unit's own open access point (**SSID `mushpi-provision-XXXX`**, address **`192.168.4.1:5000`**). The unit page is the landing page there, and the Wi-Fi setup form is a link from it.
2. **Joined to a Wi-Fi network, no hub.** The unit keeps regulating, and its readings and settings are reachable on that network at the unit's IP address.
3. **Joined to a Wi-Fi network with a Mushroom Pi hub.** The hub aggregates the units on the network, stores their readings, and organises recipes and harvests. The dashboard becomes the primary surface; the unit page remains available at the unit's own address.

| What | No Wi-Fi | Wi-Fi, no hub | Wi-Fi + hub |
|---|---|---|---|
| Climate regulation | ✅ always | ✅ always | ✅ always |
| Unit page (readings, targets, control) | ✅ on its own AP | ✅ on the network | ✅ on the network |
| Dashboard | — | — | ✅ |
| Reading history | — | — | ✅ stored by the hub |
| Batches & recipes | — | — | ✅ hub-only |

Aggregation, reading history and batches/recipes are **hub-only** — an offline unit, or one on a network without a hub, has no dashboard, no history and no batches. The unit itself never needs any of these to keep growing: it regulates from the targets it holds and serves its own page in all three modes.

### Firmware requirements

For `<device_name>.local` mDNS resolution to work (so you can reach the unit at `http://<device_name>.local:5000/ping` from the same LAN without knowing its IP), the Pico must run **MicroPython v1.26.0 or newer**.

- **v1.25.0 stable** (2025-04-15) contains the mDNS fix ([PR #17057](https://github.com/micropython/micropython/pull/17057)), but **pre-release/preview builds of v1.25.0 do NOT** (they were built before the fix landed).
- **v1.26.0** is the recommended minimum and the version confirmed working on this project.
- **AP-mode mDNS is unsupported** ([#10957](https://github.com/micropython/micropython/issues/10957)) — only STA mode works.
- **mDNS is link-local**: it only resolves on the same subnet. If the hub is on a different VLAN/subnet, you need an mDNS reflector on the router to cross it.

If the firmware is too old, the boot sequence prints a warning referring you here. The DHCP hostname and the REST API on the unit's IP still work regardless.

**Download v1.26.0 firmware**: [RPI_PICO2_W-20250809-v1.26.0.uf2](https://micropython.org/resources/firmware/RPI_PICO2_W-20250809-v1.26.0.uf2)  
**All releases**: https://micropython.org/download/RPI_PICO2_W/

### Unit status and control page

The unit serves its own status and control page, so you can check readings and change setpoints straight from a browser — no hub and no internet connection required.

- **Open the unit's address in a browser** — `http://<unit-ip>:5000` on the same network (or in the unit's own access point). The bare address serves the page to a browser and the JSON API to non-browser clients such as the hub. If your network resolves `<device_name>.local`, the page is also reachable by that name — see Firmware requirements above.
- **The unit page is the landing page in every mode.** `GET /` serves the page to a browser whether the unit is offline (its own access point) or connected; `GET /ui` is an alias for the same page.
- **Wi-Fi setup is a separate form at `GET /provision`**, linked from the unit page's offline banner. It works in both modes: offline it submits, saves the credentials and reboots the unit into the network; on a connected unit the form still loads but its submission is rejected.
- The page has **Controls** and **Devices** cards — this is where your targets, the control-loop state and any hand-set outputs show up (see [Saved settings survive a restart](#saved-settings-survive-a-restart); further reading: [Unit page documentation](https://mushroom-pi.mintlify.site/hardware/unit-page)).

### Hub address (`hub_url`)

`config.json`'s `hub_url` is the hub's **address only** — scheme, host and port:

```json
"hub_url": "http://192.168.1.50:3000"
```

- Do **not** append an API path; the unit adds the registration path (`/v1/pico-units/announce`) itself.
- Units already flashed with the full endpoint URL keep working — a value that already ends in the announce path is used unchanged.
- Surrounding whitespace and trailing slashes are ignored.
- An empty value is valid: the unit runs standalone (no hub) and prints `announce: skipped (hub_url not configured)` on the serial console at announce time.

### LED indicator

The Pico's onboard LED is the unit's one-word status signal: it comes on at power-on, then settles into one of the states below.

| LED Pattern | Meaning |
|-------------|---------|
| SLOW DOUBLE-BLINK (200/200/200/800ms) | **The unit is working** — regulating and serving its page — but has no usable Wi-Fi link. This is the offline operating state: no network configured, a failed connection, a force-provision boot, or a runtime link drop (during which the unit brings its own access point back up). Control continues throughout |
| SOLID | Wi-Fi connected, but the hub announcement is in progress or not yet acknowledged — a unit that reaches Wi-Fi but is never acknowledged by a hub stays lit |
| HEARTBEAT (even blink) | **Acknowledged by the Mushroom Pi hub** — healthy operation within a hub |
| CONFIG ERROR (3 fast blinks, 1s pause) | config.json is malformed, has invalid values, or assigns the same GPIO to two device roles — check serial output for details (duplicates name the colliding GPIO and both `pins.<role>` names) |

### Provisioning (First-Time Setup or Wi-Fi Recovery)

On first boot (no Wi-Fi configured) or after a Wi-Fi connection failure, the Pico enters **AP provisioning mode**:

1. The LED starts a **slow double-blink** pattern (200ms on / 200ms off / 200ms on / 800ms off).
2. The Pico creates its own open Wi-Fi network: **`mushpi-provision-XXXX`** (where XXXX is the last 4 hex digits of the AP MAC address — no password).
3. Connect to that network from any phone, laptop, or tablet.
4. Open **`http://192.168.4.1:5000`** in a browser — you land on the unit's status & control page.
5. Open the **Wi-Fi setup form** — linked from the page's offline banner, or directly at **`http://192.168.4.1:5000/provision`** — and enter your Wi-Fi network name (SSID) and password.
6. The Pico saves the credentials to `config.json`, reboots, and connects to your network.

**Force-provision**: Hold **GP0 to GND** during boot to force provisioning mode, even if Wi-Fi credentials are already configured. This is useful for changing networks or recovering from bad credentials. Release GP0 before the unit restarts — otherwise the next boot will force-provision again.

### WiFi Auto-Reconnection

If WiFi drops for any reason (router restart, signal interference, etc.), the Pico handles it automatically without human intervention:

1. **Detection**: The onboard watchdog monitors the WiFi link every 5 seconds. Within ~5s of a disconnection, the LED switches to the **slow double-blink** (the offline state) and the Pico prints a diagnostic message to the serial console.
2. **Reconnection attempts**: It tries to reconnect immediately, retrying with increasing delays: **5 seconds → 10 seconds → 30 seconds → 60 seconds** (stays at 60s until the link returns). All of the configured WiFi networks (primary SSID + any fallback `networks` in `config.json`) are tried in order on each attempt.
3. **Recovery**: When reconnection succeeds, the Pico refreshes its cached network info, prints the new IP, and resumes the **heartbeat** LED. It does not spawn a fresh announcement: if the boot announcement has not yet been acknowledged, its retrying task carries the new IP on the next attempt; if it was already acknowledged at boot, the hub learns a new IP through the server's regular 60-second poll instead.
4. **Control continues**: Throughout the entire outage, the grow tent regulation (humidity/temperature control, relay actuation) keeps running independently. The Pico does **not** reboot on WiFi loss — only the connection is affected.

**What you'll notice**: If your server dashboard shows a unit as "unreachable" but the relays are still clicking and the sensor values look normal, the Pico is fine — wait up to 60 seconds and it should reconnect on its own.

### Watchdog Timer (Auto-Recovery from Freezes)

The Pico has a hardware watchdog timer that acts as a dead man's switch: if the control loop freezes or hangs for more than **8 seconds**, the Pico automatically hard-reboots. After the reboot, it reconnects to WiFi, re-announces to the server, and resumes normal operation — all without human intervention.

The watchdog is armed at boot, **before the control-loop and announcement tasks start** (only the idle-meter task precedes it); the boot announcement then retries in the background with backoff (30/30/60/120/300 seconds, capped at 300) until the hub accepts it, so **an unreachable hub never affects regulation** — the unit keeps controlling humidity and temperature and serving its REST API throughout.

This covers rare but critical failures like memory corruption, unhandled exceptions, or an infinite loop. Combined with WiFi auto-reconnection, the system is designed to self-heal from most runtime failures.

### Daily Reboot (Memory Hygiene)

Embedded systems running 24/7 accumulate memory fragmentation and subtle resource leaks over days or weeks. The Pico can be configured to perform a daily soft-reboot at a quiet hour to keep things fresh:

1. **Configuration** — add a `reboot` section to `config.json`:
   ```json
   "reboot": { "enabled": true, "hour": 4, "minute": 0 }
   ```
   (Disabled by default. Also accepts `ntp_host` and `ntp_tz_offset_hours` for timezone adjustment.)

   **Timezone semantics** — the unit has no battery-backed clock: it takes its time from NTP and stores **UTC plus `ntp_tz_offset_hours`**. The configured `hour`/`minute` are compared against that value, so with `ntp_tz_offset_hours: 0` (the default) **the configured hour is a UTC hour** — set it to your own UTC offset to have `hour` mean your local time. Example: with `ntp_tz_offset_hours: 2`, `"hour": 4` reboots at 04:00 local (02:00 UTC). The offset is a whole number of hours only (valid −12…+14), so half-hour zones cannot be expressed, and **there is no daylight-saving handling** — the offset must be changed by hand when your zone shifts. Changing it takes effect on the next reboot.

   To see the clock the scheduler actually compares against, paste this at the MicroPico console:

   ```python
   import time; print("%04d-%02d-%02d %02d:%02d:%02d" % time.localtime()[:6])
   ```

2. **How it works** — After WiFi connects, the Pico syncs its clock via NTP. Every 60 seconds it checks the current time. When the configured hour and minute match, it gracefully shuts down (turns off all relays, disconnects WiFi), then performs a `machine.soft_reset()`. The reboot takes ~5–10 seconds from shutdown to full recovery.

3. **Cumulative uptime** — Unlike power-cycles or watchdog reboots which reset the uptime counter, scheduled reboots **preserve** cumulative uptime in a `uptime.json` file on flash. The dashboard's `uptime.total_s` continues counting across scheduled reboots, so you won't see alarming "just restarted" numbers every morning.

4. **Guards** — Won't reboot twice in the same day, and won't reboot before NTP syncs (no clock = no scheduled reboot).

The `GET /health` and `GET /system` endpoints report `uptime.reboot_reason` so you can tell at a glance whether the last restart was scheduled, a watchdog recovery, or an unexpected power cycle.

### Saved settings survive a restart

The unit remembers the grower's own settings across **every kind of restart** — power cut, soft or hard reboot, the scheduled daily reboot, and a watchdog-triggered recovery. They are stored on the unit itself, in a separate device-local store from the pin/provisioning configuration (`config.json`); the separation is deliberate, so re-flashing provisioning no longer risks the grower's saved targets, and pin-mapping persistence is unchanged.

| What | Survives a restart? |
|---|---|
| Temperature & humidity targets | **Yes** — restored on boot |
| Control-loop on/off state | **Yes** — the loop resumes as it was left |
| Hand-set fan + humidifier positions | **Yes, while the loop is off** |
| Heater | **Never** — always comes back **off** |

The heater rule is a safety one: nothing on the unit cools it, and with the loop off nothing regulates temperature, so a heater restored to *on* would be an unattended heat source. Hand-setting the heater on still works while the unit is running — it simply never survives a restart.

Only the grower's own actions are saved — switching driven by the control loop never writes to storage. A setting becomes durable as soon as the action succeeds, and the write is atomic, so a power cut mid-write leaves the previous value intact rather than a corrupt store. Turning the loop back on takes over from hand-set positions, as before. After a power cut, the restored targets govern until the unit reconnects to a hub running a batch, at which point the batch's targets take over as usual.

One caveat: turning the loop **off** arms a one-shot "all outputs off" that fires on the loop's next pass, so a fan or humidifier hand-set in that brief window is switched off once; hand-sets made afterwards hold while the loop stays off. It never affects the saved settings.

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

# 🍄 Mushroom Pi 🍓 - Pico Growing Unit

![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)![Raspberry Pi](https://img.shields.io/badge/-Raspberry_Pi-C51A4A?style=for-the-badge&logo=Raspberry-Pi)

A **MicroPython** repo for **Raspberry Pi Pico 2 W** to:

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

This software allows minimal communication through the Pico's integrated LED. When the software starts executing, the LED will ALWAYS turn on. After that first flash, the possible LED statuses are:

| LED Pattern | Meaning |
|-------------|---------|
| OFF | Wi-Fi not connected / booting |
| SOLID | Wi-Fi connected, awaiting server announce |
| HEARTBEAT (even blink) | Normal operation |
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

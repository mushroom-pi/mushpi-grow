# Hardware Reference — Mushroom Pi Growing Unit

Bill of materials, pin map, wiring reference, and power analysis for the Raspberry Pi Pico 2W growing unit.

## Bill of Materials

| # | Component | Model / Variant | URL | Notes |
|---|-----------|----------------|-----|-------|
| 1 | Microcontroller | Raspberry Pi Pico 2W | — | RP2350, dual-core, WiFi |
| 1 | Temperature & humidity sensor | DHT11 (standard, 4-pin module) | [tiendatec.es](https://www.tiendatec.es/maker-zone/sensores/588-sensor-dht11-temperatura-y-humedad-para-arduino-8405881480002.html) | Digital, single-wire protocol. Chequered accuracy. |
| 1 | Humidifier relay | 8-channel 5V relay module (SRD-05VDC) | [tiendatec.es](https://www.tiendatec.es/maker-zone/reles/645-modulo-rele-8-canales-5v-para-arduino-8406451430007.html) | Channels 1–3 used. 5 channels spare. Active-low by default. 5V VCC required. Optocoupler isolated. |
| 3 | — (relay channels used) | Channels 1, 2, 3 on the above module | — | Channel 1 = humidifier, channel 2 = fan, channel 3 = heater |
| 1 | Fan | 30×30×10mm 5V DC brushless | [tiendatec.es](https://www.tiendatec.es/raspberry-pi/accesorios/344-ventilador-30x30x10mm-5v-compatible-raspberry-pi-8403440020003.html) | Small form factor, 5V. Used for air circulation. |
| 1 | Humidifier | Basic ultrasonic mist maker (USB-powered) | [amazon.es](https://www.amazon.es/dp/B09BTKCTMC) | Placed in water reservoir. Toggled via relay channel 1. |
| 1 | Heating mat | USB-powered heating mat (low wattage, 5V) | — | Toggled via relay channel 3. USB cable cut, 5V line interrupted by relay COM3/NO3. Low-voltage — no mains in the grow chamber. |
| 1 | Power supply | 5V 2A+ DC power supply (mandatory) | — | Dedicated PSU for relay module and all actuators. Not run through the Pico's USB port. |

## Pin Map

| GPIO | Component | Signal | Notes |
|------|-----------|--------|-------|
| 0 (GP0) | Force-provision input | — | Internal pull-up (33kΩ–60kΩ). Hold LOW at boot (momentary switch to GND) to force AP provisioning mode. |
| 4 (GP4) | DHT11 | DATA | Single-wire digital. Internal pull-up used. |
| 6 (GP6) | Relay channel 1 (humidifier) | IN1 | Active-low by default. Configurable via `active_high` in `config.json`. |
| 7 (GP7) | Relay channel 2 (fan) | IN2 | Active-low by default. |
| 8 (GP8) | Relay channel 3 (heater) | IN3 | Active-low by default. |
| `LED` | Onboard LED | — | Status indicator. OFF = no WiFi. SOLID = WiFi connected, hub not confirmed. BLINKING = fully operational. SLOW DOUBLE-BLINK (200/200/200/800ms) = AP provisioning mode. |

### Available GPIOs

The following pins are **unused** and available for expansion:

- GPIO 1, 2, 3, 5, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 26, 27, 28
- All ADC pins (26, 27, 28) — usable for analog sensors
- I2C0 (GP1 only; GP0 reserved for force-provision) and I2C1 (GP2/GP3) — available for I2C sensors
- SPI0 and SPI1 — available for SPI devices

## Wiring Reference

### DHT11
- **VCC** → Pico 3V3 (pin 36)
- **GND** → Pico GND (any)
- **DATA** → GPIO 4 (pin 6)
- Internal pull-up enabled in firmware.
- Note: DHT11 accuracy is ±2°C temperature, ±5% humidity. Consider DHT22 for better accuracy in production.

### Relay Module (8-channel)
- **VCC** → External 5V (currently USB, should be dedicated PSU)
- **GND** → Pico GND (common ground)
- **JD-VCC** → Jumper set to use shared VCC (remove jumper if using separate relay coil power)
- **IN1** (channel 1) → GPIO 6 (humidifier)
- **IN2** (channel 2) → GPIO 7 (fan)
- **IN3** (channel 3) → GPIO 8 (heater)
- **IN4–IN8** → Unused (available for expansion)
- Active-low operation: relay engages when GPIO is LOW. Can be inverted via `active_high: true` in `config.json`.

### Fan (5V DC)
- Switched via relay channel 2.
- Fan positive → Relay COM2. Relay NO2 → 5V. Fan negative → GND.
- Alternatively: Fan positive → 5V. Fan negative → Relay NO2. Relay COM2 → GND.

### Humidifier (USB mist maker)
- Switched via relay channel 1.
- USB cable cut. 5V line interrupted by relay NO1/COM1.

### Heating Mat (USB-powered, 5V)
- Switched via relay channel 3.
- USB cable cut. 5V line interrupted by relay COM3/NO3. GND passes through uninterrupted. Data lines cut and insulated.
- Same wiring method as the USB humidifier and fan.

## Power

### Current Status
All components (Pico 2W, relay module logic, fan, humidifier, heating mat) are powered via 5V DC. The heating mat, fan, and humidifier each use a cut USB cable with the 5V line switched through a relay channel. All share a common 5V rail.

### Known Issues
- **Relay module** requires 5V for coil power. The 8-channel module's coils draw ~70mA per active channel (×3 channels = ~210mA). Combined with Pico 2W (~100mA WiFi active), fan (~100mA), humidifier (~300mA), and heating mat (~400–1000mA), total draw can approach **~1.7A**. A standard USB 2.0 port (500mA) is inadequate. **A dedicated 5V 2A+ DC power supply is mandatory.**
- No fuse or reverse polarity protection.
- No dedicated power filtering or decoupling capacitors.

### Power Supply Requirement
- A dedicated 5V 2A+ DC power supply with barrel jack is **mandatory** for operation with all three actuators (fan, humidifier, heating mat).
- Power the Pico via VSYS (pin 39) or USB. Power the relay module VCC directly from the PSU (bypassing the Pico's 3.3V regulator).
- Add a 500mA polyfuse on the 5V rail.
- Consider a 100µF electrolytic capacitor across the relay VCC/GND for coil transient suppression.

## Component Lifecycle

| Component | GPIOs needed | Protocol | Current draw | Voltage |
|-----------|-------------|----------|-------------|---------|
| DHT11 | 1 (digital) | Proprietary single-wire | ~2.5mA | 3.3V |
| Relay module (per active channel) | 1 (digital) | GPIO HIGH/LOW | ~70mA coil | 5V |
| Fan | 1 (via relay) | — | ~100mA | 5V |
| Humidifier | 1 (via relay) | — | ~300mA (estimated) | 5V USB |
| Heating mat | 1 (via relay) | — | ~400–1000mA (estimated) | 5V USB |

## Schematics

Mermaid wiring and system block diagrams are maintained in `mushpi-docs/hardware/schematics/`:

- [Wiring diagram](../mushpi-docs/hardware/schematics/wiring-diagram.md) — every physical connection, color-coded by function
- [System block diagram](../mushpi-docs/hardware/schematics/system-block-diagram.md) — high-level architecture: Pico ↔ Server ↔ Actuators

Key elements covered:
- Pico 2W pinout (GP0 force-provision, GP4 DHT11, GP6–GP8 relays, VBUS, 3V3 OUT)
- DHT11 wiring (VCC, DATA, GND)
- Relay module internal circuit per channel: optocoupler isolation, NPN driver, flyback diode
- Actuators: USB mist maker (5V), DC fan (5V), USB heating mat (5V)
- Force-provision button (GP0 → GND)
- Power section: dedicated 5V 2A PSU, 3.3V rail, decoupling capacitors, polyfuse

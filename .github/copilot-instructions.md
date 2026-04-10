# Copilot Instructions — mushpi-grow (Pico Growing Unit)

## Project Context

`mushpi-grow` is a **MicroPython** firmware for a **Raspberry Pi Pico 2W**. Each physical growing unit runs this firmware. Its two responsibilities are:
1. **Control** the ambient conditions (humidity and temperature) by toggling a humidifier, fan, and heating mat.
2. **Serve** real-time sensor readings and accept commands via a lightweight REST API over Wi-Fi.

This component is part of the larger **Mushroom Pi** system:
- Each Pico unit self-announces to `mushpi-server` (NestJS hub, typically at `hub_url` in `config.json`) on boot.
- `mushpi-server` polls each unit's REST API periodically and stores historical readings.
- `mushpi-client` (React frontend) visualises readings and sends commands through `mushpi-server`.

## Hardware

| Component | Purpose | Default GPIO |
|-----------|---------|-------------|
| DHT11 sensor | Temperature + humidity | 4 |
| Humidifier relay | Raises humidity | 6 |
| Fan relay | Lowers humidity (ventilation) | 7 |
| Heating mat relay | Raises temperature | 8 |
| Onboard LED | Status indicator | `LED` |
| Wi-Fi (CYW43) | Network connectivity | built-in |

**Active-low relays by default** (`"active_high": false` in `config.json`). The `IO` class in `app/hw.py` handles `_on()`/`_off()` polarity automatically.

### LED Status Codes
- **ON solid** — Wi-Fi connected, hub not yet reached.
- **BLINKING (heartbeat)** — Wi-Fi connected and hub successfully announced.
- **OFF** — Wi-Fi connection failed.

## Software Architecture

```
main.py
 ├─ load_config()           # reads config.json
 ├─ IO()                    # GPIO wrapper (app/hw.py)
 ├─ DHTReader()             # sensor wrapper (app/sensor.py)
 ├─ connect_wifi()          # app/wifi.py
 └─ asyncio.run(main())
      ├─ Task: announce_then_retry_once()   # app/announce.py — POST to hub on boot
      ├─ Task: control_loop()              # app/control.py — PID-like hysteresis loop
      └─ Await: start_server()             # app/api.py — Microdot HTTP server
```

The entire program runs on **uasyncio** (MicroPython's asyncio). Never use blocking calls inside async tasks longer than a few ms; use `await asyncio.sleep_ms()`.

### Key Modules

| Module | Role |
|--------|------|
| `app/state.py` | Shared mutable state: `status`, `setpoints`, `devices`, `control_enabled`, `_system_info` |
| `app/hw.py` | `IO` class — GPIO init, `hum_on/off`, `fan_on/off`, `heat_on/off`, LED control, `all_off()` |
| `app/sensor.py` | `DHTReader` — wraps DHT11, updates `status["temperature"]` and `status["humidity"]` |
| `app/control.py` | `control_loop()` — hysteresis-based control; reads `setpoints`, writes via `IO` |
| `app/announce.py` | `announce_then_retry_once()` — POST system info to hub; uses `urequests` |
| `app/api.py` | `make_app()` / `start_server()` — Microdot REST API |
| `app/metrics.py` | `system_snapshot()` — memory, filesystem, uptime |
| `app/config_loader.py` | Loads and validates `config.json` |
| `app/shutdown.py` | `graceful_shutdown()` + `stop_event` for clean teardown |

## REST API Endpoints

All responses are JSON. Served on `api_port` (default `5000`).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Full snapshot: system, health, devices, sensors, outputs, setpoints, control |
| GET | `/ping` | Empty 200 — liveness check |
| GET | `/health` | Memory, FS, uptime, CPU temp via `system_snapshot()` |
| GET | `/system` | Static system info (board, MicroPython version, Wi-Fi MAC/IP) |
| GET | `/sensors` | Latest DHT reading (`?force=1` triggers a fresh measurement) |
| GET/POST | `/setpoints` | Get or set `{"temperature": int, "humidity": int}` |
| GET/POST | `/outputs` | Get or manually set `{"fan": bool, "humidifier": bool, "heater": bool}` |
| GET/POST | `/control` | Get or set `{"enabled": bool}` — enables/disables the control loop |
| GET/POST | `/setup` | Get or remap device GPIO pins and `active_high` polarity |

## Control Loop Logic (app/control.py)

Runs every `control.period_s` seconds (default 10 s).

**Humidity control** (hysteresis `hyst_hum`, default ±5%):
- `humidity < setpoint - hyst` → turn humidifier ON, fan OFF
- `humidity > setpoint + hyst` → turn fan ON, humidifier OFF
- In range → both OFF

**Temperature control** (hysteresis `hyst_temp`, default ±1°C):
- `temperature < setpoint - hyst` → heater ON
- `temperature > setpoint + hyst` → heater OFF
- In range → heater OFF

When `control_enabled = False`, all outputs are turned off once via `io.all_off()`, then the loop sleeps until re-enabled.

## Configuration (config.json)

```json
{
  "wifi": { "ssid": "...", "password": "..." },
  "hub_url": "http://<hub_ip>:<hub_port>/pico-units",
  "device_name": "pico-unit1",
  "api_port": 5000,
  "control": { "period_s": 10, "hyst_hum": 5, "hyst_temp": 1 }
}
```

`hub_url` must point to the `POST /pico-units` endpoint on `mushpi-server`.

## Required Libraries (not in MicroPython stdlib)

Upload to `/lib/` on the Pico or install via `mip`:
- `microdot.py` — HTTP server ([source](https://github.com/miguelgrinberg/microdot))
- `urequests.py` — HTTP client ([source](https://github.com/lucien2k/wipy-urllib))

## Development Conventions

- **No type hints** — MicroPython has limited support; avoid them.
- **Avoid large imports** — flash/RAM are constrained (~264 KB RAM on RP2040).
- **State is global** — the `app/state.py` dictionaries (`status`, `setpoints`, `devices`) are shared across modules via import. Never replace these dicts; mutate them in place.
- **Async-first** — all I/O operations should be `async def` coroutines; use `asyncio.create_task()` for background work.
- **Error handling** — use bare `except:` (MicroPython doesn't always support `except Exception as e` cleanly for all error types). Always log with `print()`.
- **Deployment** — use the [MicroPico VSCode extension](https://marketplace.visualstudio.com/items?itemName=paulober.pico-w-go) to upload files to the device.
- **VERSION file** — bump the version string here on every release; it is read at runtime by `state.init_software_info_from_file()`.

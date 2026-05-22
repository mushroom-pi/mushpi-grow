# mushpi-grow — Pico Growing Unit Agent

MicroPython firmware for Raspberry Pi Pico 2W. Controls humidity/temperature via hysteresis and serves a REST API on port 5000.

## Hardware Pins (defaults)

| Device | GPIO | Notes |
|--------|------|-------|
| DHT11 | 4 | Temp + humidity sensor |
| Humidifier relay | 6 | Active-low by default |
| Fan relay | 7 | Active-low by default |
| Heating mat relay | 8 | Active-low by default |
| Onboard LED | `LED` | Status indicator |

`active_high` in `config.json` flips relay polarity. The `IO` class in `app/hw.py` handles this automatically — never toggle GPIO directly.

## Module Map

| Module | Role |
|--------|------|
| `app/state.py` | Shared dicts: `status`, `setpoints`, `devices`, `control_enabled` — mutate in place, never replace |
| `app/hw.py` | `IO` — GPIO init, `hum_on/off`, `fan_on/off`, `heat_on/off`, `all_off()` |
| `app/sensor.py` | `DHTReader` — reads DHT11, updates `status` |
| `app/control.py` | `control_loop()` — hysteresis-based coro |
| `app/announce.py` | `announce_then_retry_once()` — POSTs to hub on boot |
| `app/api.py` | Microdot REST API |
| `app/metrics.py` | `system_snapshot()` — RAM, FS, uptime |

## Control Loop Logic

Runs every `control.period_s` (default 10 s) as a `uasyncio` coroutine.

- **Humidity**: below `setpoint − hyst_hum` → humidifier ON, fan OFF; above `setpoint + hyst_hum` → fan ON, humidifier OFF; in range → both OFF.
- **Temperature**: below `setpoint − hyst_temp` → heater ON; above `setpoint + hyst_temp` → heater OFF.
- When `control_enabled = False`: call `io.all_off()` once, then sleep until re-enabled.

## REST API (port 5000)

| Method | Path | Notes |
|--------|------|-------|
| GET | `/` | Full snapshot (polled by mushpi-server cron) |
| GET | `/ping` | Liveness |
| GET | `/health` | RAM, FS, uptime, CPU temp |
| GET | `/system` | Board/Wi-Fi info |
| GET/POST | `/sensors` | DHT reading; `?force=1` triggers fresh read |
| GET/POST | `/setpoints` | `{"temperature": int, "humidity": int}` |
| GET/POST | `/outputs` | `{"fan": bool, "humidifier": bool, "heater": bool}` |
| GET/POST | `/control` | `{"enabled": bool}` |
| GET/POST | `/setup` | GPIO pin mapping + `active_high` |

## config.json

```json
{
  "wifi": { "ssid": "...", "password": "..." },
  "hub_url": "http://<hub_ip>:3000/pico-units",
  "device_name": "pico-unit1",
  "api_port": 5000,
  "control": { "period_s": 10, "hyst_hum": 5, "hyst_temp": 1 }
}
```

`device_name` must match the `handle` field in `mushpi-server`'s `PicoUnit` entity.

## Coding Rules

- **No type hints** — MicroPython support is limited.
- **Never use blocking calls** inside async tasks; use `await asyncio.sleep_ms()`.
- **All I/O is async** — use `asyncio.create_task()` for background work.
- **Error handling**: bare `except:` is acceptable; always log with `print()`.
- **RAM budget**: ~264 KB on RP2040; avoid large imports or allocations.
- **Required libs** (upload to `/lib/`): `microdot.py`, `urequests.py`.
- **Deploy** with the MicroPico VSCode extension.
- **VERSION file**: bump on every release; read at runtime by `state.init_software_info_from_file()`.

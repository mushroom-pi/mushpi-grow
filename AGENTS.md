# mushpi-grow — Pico Growing Unit

MicroPython firmware for Raspberry Pi Pico 2W. Controls humidity and temperature via hysteresis, serves a REST API on port 5000, and announces itself to a central hub on boot. See also `.github/copilot-instructions.md` for the original Copilot agent instructions.

## Build, Lint, Test

There are no traditional build or test commands — this is plain MicroPython uploaded directly to the Pico. To deploy:

- Use the **MicroPico** VSCode extension (`paulober.pico-w-go` in `.vscode/extensions.json`).
- Upload all `.py` files, `config.json`, and `VERSION` to the Pico's flash.
- Required third-party libs go in `/lib/` on the device: `microdot.py`, `urequests.py` (already vendored in the repo).
- Bump `VERSION` on every release; it is read at runtime by `state.init_software_info_from_file()`.

## Project Structure

```
mushpi-grow/
├── main.py              # Entry point — wires everything together
├── config.json          # WiFi, hub URL, device name, control params (gitignored)
├── VERSION              # Single-line version string (e.g. "0.1.0")
├── app/
│   ├── state.py         # Shared mutable dicts (status, setpoints, devices, control_enabled)
│   ├── hw.py            # IO class — GPIO init, relay control, LED heartbeat
│   ├── sensor.py        # DHTReader — reads DHT11, updates status
│   ├── control.py       # control_loop() — hysteresis-based async coroutine
│   ├── announce.py      # announce_then_retry_once() — POSTs presence to hub on boot
│   ├── api.py           # Microdot REST API (port 5000)
│   ├── metrics.py       # system_snapshot() — RAM, FS, Wi‑Fi RSSI, MCU temp, uptime, loop util
│   ├── wifi.py          # connect_wifi() — blocking STA connect with hostname
│   ├── config_loader.py # load_config() — shallow-merges config.json over defaults
│   └── shutdown.py      # graceful_shutdown() — stop event, all_off, WiFi disconnect
└── lib/
    ├── microdot.py      # Microdot async web framework (vendored)
    └── urequests.py     # MicroPython HTTP client (vendored)
```

## Startup Sequence (`main.py`)

1. `IO()` — initialises GPIO, turns onboard LED ON.
2. `DHTReader()` — initialises DHT11 sensor.
3. `load_config()` — reads `config.json` with shallow merge over defaults.
4. `state.init_system_info(cfg)` — caches board/MicroPython/build metadata.
5. `connect_wifi()` — blocking STA connect (up to ~10 s); LED stays OFF until connected, then LED ON.
6. `state.attach_wlan_info(wlan, cfg)` — caches MAC, IP, hostname, port.
7. `main()` async task:
   - `start_metrics()` — launches background event-loop utilisation meter.
   - Spawns `announce_then_retry_once()` — tries POST to hub; if it fails, waits 60 s and retries once.
   - Spawns `control_loop()` — hysteresis loop.
   - `await start_server()` — Microdot HTTP server on `0.0.0.0:<api_port>`.

## Hardware Pins (defaults)

| Device           | GPIO | Notes                         |
|------------------|------|-------------------------------|
| DHT11            | 4    | Temperature + humidity sensor |
| Humidifier relay | 6    | Active-low by default         |
| Fan relay        | 7    | Active-low by default         |
| Heater relay     | 8    | Active-low by default         |
| Onboard LED      | `LED`| Status indicator              |

- `active_high` in `config.json` flips relay polarity. The `IO` class in `app/hw.py` handles this automatically — never toggle GPIO directly.
- Pins can be remapped at runtime via `POST /setup`.

## Control Loop Logic

Runs as a `uasyncio` coroutine every `control.period_s` seconds (default 10 s in copilot instructions, 30 s in the checked-in `config.json`, default 5 s in `config_loader.py`).

- **Humidity**: below `setpoint + hyst_hum` → humidifier ON, fan OFF. At or above `setpoint + hyst_hum` → fan ON, humidifier OFF.
- **Temperature**: below `setpoint + hyst_temp` → heater ON. At or above `setpoint + hyst_temp` → heater OFF.
- When `control_enabled = False`: calls `io.all_off()` once, then sleeps in 100 ms chunks until re-enabled.

## REST API (port 5000)

| Method     | Path        | Notes                                                  |
|------------|-------------|--------------------------------------------------------|
| GET        | `/`         | Full snapshot (system + health + sensors + outputs + setpoints + control state) |
| GET        | `/ping`     | Liveness (empty 200)                                   |
| GET        | `/health`   | RAM, FS, Wi‑Fi RSSI, MCU temp, uptime, loop util %     |
| GET        | `/system`   | Board, MicroPython version, software version, Wi‑Fi IP/MAC |
| GET / POST | `/sensors`  | DHT reading; `?force=1` triggers on-demand measurement |
| GET / POST | `/setpoints`| `{"temperature": int, "humidity": int}`                 |
| GET / POST | `/outputs`  | `{"fan": bool, "humidifier": bool, "heater": bool}`     |
| GET / POST | `/control`  | `{"enabled": bool}`                                    |
| GET / POST | `/setup`    | GPIO pin mapping + `active_high`                        |

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

- `device_name` must match the `handle` field in `mushpi-server`'s `PicoUnit` entity.
- `config.json` is gitignored — a checked-in copy with real credentials exists locally.
- `config_loader.py` provides defaults for all fields (device_name: `"PicoDevice"`, period_s: `5`).

## LED Status Indicators

- **OFF**: WiFi connection failed — check SSID/password.
- **SOLID ON**: WiFi connected but hub announcement failed (or still in progress).
- **BLINKING** (heartbeat): WiFi connected and hub announcement succeeded.

## Shared State Rules

- `app/state.py` exports mutable dictionaries: `status`, `setpoints`, `devices`, `control_enabled`.
- **Always mutate in place** — never reassign these module-level names.
- `_system_info` is cached (built once, read many times).

## Coding Rules

- **No type hints** — MicroPython support is limited; CPython type annotations are stripped before upload.
- **Never use blocking calls** inside async tasks; use `await asyncio.sleep_ms()`.
- **All I/O is async** — use `asyncio.create_task()` for background work.
- **Error handling**: bare `except:` is acceptable; always log errors with `print()`.
- **RAM budget**: ~264 KB on RP2040; avoid large imports, f-strings, or unnecessary allocations.
- **Required libs** (upload to `/lib/` on the Pico): `microdot.py`, `urequests.py`.
- **Graceful shutdown**: use `stop_event` from `app/shutdown.py` — loops should check it and exit cleanly. The `finally` block in `main.py` calls `graceful_shutdown()` which turns all outputs off, disconnects WiFi, and deinits the sensor.
- **MicroPython quirks**:
  - `urequests` does not support the `json=` kwarg — pass pre-serialised `ujson.dumps(body)` as `data=` with explicit `Content-Type: application/json` header.
  - `usocket.setdefaulttimeout()` is used to set per-request timeouts and restored to `None` afterwards.
  - DHT11 can return implausible readings; `DHTReader.plausible()` validates `-10 ≤ t ≤ 60` and `0 ≤ h ≤ 100`.

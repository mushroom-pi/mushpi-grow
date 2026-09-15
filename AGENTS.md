# mushpi-grow — Pico Growing Unit

MicroPython firmware for Raspberry Pi Pico 2W. Controls humidity and temperature via hysteresis, serves a REST API on port 5000, and announces itself to a central hub on boot.

> **Reference**: long-tail details (startup sequence, control-loop algorithm, `config.json` schema, hardware pins, AP provisioning, LED states, MicroPython quirks, RAM/gc) live in [`REFERENCE.md`](./REFERENCE.md). Load it **only when the task touches those areas** — do not read it on every spawn.

## Build, Lint, Test

There are no traditional build or test commands — this is plain MicroPython uploaded directly to the Pico:

- Deploy via the **MicroPico** VSCode extension (`paulober.pico-w-go` in `.vscode/extensions.json`). Upload all `.py` files, `.html` files (e.g. `app/provision.html`), and `config.json` to the Pico's flash.
- Required third-party libs go in `/lib/` on the device: `microdot.py`, `urequests.py` (present in `lib/` locally but gitignored — not tracked in git).
- Bump `_SOFTWARE_VERSION` in `app/state.py` only when releasing (on the `main` branch), never during day-to-day `dev` work — it is the single source of truth for the firmware version (returned by `GET /system` and `GET /`). Keep `spec/openapi.yaml` `info.version` equal to it, and keep the mock's version in sync. `_API_VERSION` in the same file is the Pico↔Server REST contract generation — bump it only on breaking contract changes (additive changes do not bump it); it is the one exception to the release gate and may move on `dev`, and whenever it moves `_SOFTWARE_VERSION` must also get at least a MINOR bump.

## Project Structure

```
mushpi-grow/
├── main.py              # Entry point — wires everything together
├── config.json          # WiFi, hub URL, device name, control params (gitignored)
├── README.md           # Setup + deployment instructions
├── HARDWARE.md         # Canonical BOM, pin map, wiring, power (maintained by mushpi-electronics)
├── app/
│   ├── state.py         # Shared mutable dicts (status, setpoints, devices)
│   ├── hw.py            # IO class — GPIO init, relay control, LED heartbeat
│   ├── sensor.py        # DHTReader — reads DHT11, updates status
│   ├── control.py       # control_loop() — hysteresis-based async coroutine
│   ├── announce.py      # announce_then_retry_once() — POSTs presence to hub (boot + runtime re-announce); idempotent/re-invocable
│   ├── api.py           # Microdot REST API (port 5000)
│   ├── metrics.py       # system_snapshot() — RAM, FS, Wi‑Fi RSSI, MCU temp, uptime, loop util
│   ├── wifi.py          # connect_wifi() (blocking boot connect), reconnect_once_async() (async reconnect), wifi_watchdog_loop() (link monitor with backoff)
│   ├── config_loader.py # load_config() + save_config() — atomic config read/write
│   ├── config_validator.py # validate_config() — boot-time config field validation
│   ├── uptime.py         # Cumulative uptime tracking, boot-reason classification, uptime.json persistence
│   ├── reboot_scheduler.py # Daily scheduled soft-reboot (STA mode), NTP sync
│   ├── provision.html   # HTML form served in AP provisioning mode
│   └── shutdown.py      # graceful_shutdown() + reboot() + soft_reboot()
├── spec/
│   └── openapi.yaml     # Hand-maintained OpenAPI 3.0 spec (committed)
└── lib/
    ├── microdot.py      # Microdot async web framework (gitignored — upload to device /lib/)
    └── urequests.py     # MicroPython HTTP client (gitignored — upload to device /lib/)
```

## REST API (port 5000)

| Method     | Path        | Notes                                                  |
|------------|-------------|--------------------------------------------------------|
| GET        | `/`         | STA: full JSON snapshot. AP: HTML provisioning form. CORS headers on all responses. |
| GET        | `/ping`     | Liveness (empty 200)                                   |
| GET        | `/health`   | RAM, FS, Wi‑Fi RSSI, MCU temp, uptime, loop util %     |
| GET        | `/system`   | Board, MicroPython version, software version, Wi‑Fi IP/MAC |
| GET        | `/sensors`  | DHT reading; `?force=1` triggers on-demand measurement (via `sensor.read()`, not raw `sensor.d.measure()`) |
| GET / POST | `/setpoints`| `{"temperature": int, "humidity": int}` — triggers immediate hysteresis re-evaluation when control is enabled |
| GET / POST | `/outputs`  | `{"fan"?: bool, "humidifier"?: bool, "heater"?: bool}` — POST accepts partial updates (at least one key required); GET returns all three current states |
| GET / POST | `/control`  | `{"enabled": bool}` — disables control immediately (calls `relays_off()` sync) |
| GET / POST | `/setup`    | GPIO pin mapping + `active_high`                          |
| POST       | `/provision`| Wi-Fi credential provisioning (AP mode only — writes config.json + reboots) |
| POST       | `/reboot`   | `{"type": "soft"\|"hard"}` (optional, defaults to `soft`) — triggers graceful reboot (soft = `machine.soft_reset()`, hard = `machine.reset()`). Response returns before reboot executes. |

## Shared State Rules

- `app/state.py` exports mutable dictionaries: `status`, `setpoints`, `devices`.
- **Always mutate in place** — never reassign these module-level names.
- `_system_info` is cached (built once, read many times).
- `_SOFTWARE_VERSION` in `state.py` is the single source of truth for the firmware version. Bump it only when releasing (on `main`), keeping `spec/openapi.yaml` `info.version` and the mock in sync.
- **Relay-state keys must match exactly**: the keys in `status` (`fan`, `humidifier`, `heater`) must match the keys in `devices["pins"]` and the JSON field names returned by `GET /` (`outputs.fan`, etc.) and `GET /outputs`. The `IO` helper methods (`hum_on()`, `fan_on()`, `heat_on()`, etc.) are the **only** code allowed to write these keys. A typo in the key name (e.g. `status["heat"]` instead of `status["heater"]`) causes a silent desync: the GPIO pin toggles correctly but the status dict value stays stale, and the server will always poll `false`.
- **No GPIO read-back**: relay state in the API responses comes from the cached `status` dict, not from live GPIO reads. The dict keys are the single source of truth — keep them correct.
- **Never snapshot `status[...]` into module-level dicts at import time** — always read from `status` live inside handlers. Module-level snapshots capture initial values and never update.

## Coding Rules

- **No type hints** — MicroPython support is limited; CPython type annotations are stripped before upload.
- **Never use blocking calls** inside async tasks; use `await asyncio.sleep_ms()`.
- **WiFi runtime operations must be async**: the blocking `connect_wifi()` is boot-only and must never be called from a running task. Runtime reconnect/monitoring uses `reconnect_once_async()` and `wifi_watchdog_loop()`, which poll with `await asyncio.sleep_ms()` so the control loop and server keep running.
- **Module ownership**: `app/wifi.py` owns all WiFi state transitions (connect, reconnect, link monitoring). `app/control.py` owns sensor sampling + hysteresis only. No cross-module WiFi logic in control.py.
- **WDT (Watchdog Timer)**: STA mode only (not AP provisioning). Initialised **after** `connect_wifi()` returns (boot WiFi can take 30s+ and would trigger a spurious reboot). Fed from `control_loop`'s 200ms chunked-sleep loop — covers event-loop-wide blocking hangs and control_loop task death. Not fed from an independent task (would miss control_loop death).
- **`announce_then_retry_once` is re-entrant**: safe to call at runtime (not just boot). The caller must cancel any prior announce task before spawning a new one to avoid overlapping heartbeat/LED control.
- **HTTP handlers must not perform blocking sensor reads** (e.g. `sensor.d.measure()`) — use the last cached `status` values from `app/state.py` instead. The one exception is `GET /sensors?force=1`, which triggers an on-demand measurement via `sensor.read()`.
- **Garbage sensor readings must never leak into `status`**: `DHTReader.read()` writes `status["temperature"]` and `status["humidity"]` ONLY after `plausible()` returns `True`. Implausible readings set `last_sensor_error` but leave the last plausible values in place, so the control loop's `None` guards correctly suppress actuation during sensor faults.
- **All POST handler error responses use the shape `{"ok": False, "error": "..."}`**. Do not mix flat `{"error": "..."}` with `{"ok": False, ...}` — standardise on `{"ok": False, "error": "..."}` across `/setpoints`, `/outputs`, `/control`, `/setup`, `/provision`, and `/reboot`.
- **`POST /outputs` accepts partial updates**: individual relay keys (`fan`, `humidifier`, `heater`) are optional — only keys present in the request body are applied. At least one key must be provided. The response always returns all three current relay states.
- **`reboot_reason` enum**: the valid values produced by `app/uptime.py` are `scheduled`, `soft`, `hard`, `watchdog`, `power_on`, `unknown`. Document these in the `uptime` response schemas and keep `spec/openapi.yaml` in sync.
- **All I/O is async** — use `asyncio.create_task()` for background work.
- **Error handling**: bare `except:` is acceptable; always log errors with `print()`.
- **Graceful shutdown**: use `stop_event` from `app/shutdown.py` — loops should check it and exit cleanly. The `finally` block in `main.py` calls `graceful_shutdown()` which turns all outputs off, disconnects WiFi, and deinits the sensor.
- **Key design rule**: sensor sampling must remain independent of `control_enabled` (observability vs. actuation separation). `io.all_off()` is for fail-safe/shutdown only; use `io.relays_off()` for control-disabled state. See REFERENCE.md for the full hysteresis algorithm.

## API Specification

The Pico firmware has no automatic spec generation (MicroPython, 264 KB RAM). The REST API is documented manually in `spec/openapi.yaml` (committed, hand-maintained OpenAPI 3.0 — there is no Bruno collection; edit the YAML directly when the API changes). The mock (`mushpi-mock`) reads this spec as its canonical reference — keep it current. Out-of-band payloads such as the boot announce (POSTed to the hub, not a served path) are documented in `spec/openapi.yaml` under `components.schemas` and must never be added under `paths:`; the announce payload must stay aligned with `mushpi-mock` and the server's `AnnouncePicoUnitDto`.

# mushpi-grow — Pico Growing Unit

MicroPython firmware for Raspberry Pi Pico 2W. Controls humidity and temperature via hysteresis, serves a REST API on port 5000, and announces itself to a central hub on boot.

> **Reference**: long-tail details (startup sequence, firmware/mDNS requirements, hardware pins, control-loop algorithm, `config.json` schema, AP provisioning, LED states, MicroPython quirks, RAM/gc, persistence) live in [`REFERENCE.md`](./REFERENCE.md). Load it **only when the task touches those areas** — do not read it on every spawn.

## Build, Lint, Test

There are no traditional build or test commands — this is plain MicroPython uploaded directly to the Pico:

- Deploy via the **MicroPico** VSCode extension (`paulober.pico-w-go` in `.vscode/extensions.json`). Upload all `.py` files, `.html` files (e.g. `app/provision.html`), and `config.json` to the Pico's flash.
- Required third-party libs go in `/lib/` on the device: `microdot.py`, `urequests.py` (present in `lib/` locally but gitignored — not tracked in git).
- `_SOFTWARE_VERSION` in `app/state.py` is the firmware version's single source of truth (`GET /system`, `GET /`). Bump only on release (`main`), never during `dev`; keep `spec/openapi.yaml` `info.version` and the mock in sync. `_API_VERSION` (same file) is the Pico↔Server contract generation — bump only on breaking changes, not additive; it alone may move on `dev`, but then `_SOFTWARE_VERSION` needs ≥ a MINOR bump.

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
| GET        | `/`         | STA: full JSON snapshot. AP: HTML provisioning form. CORS on all responses. |
| GET        | `/ping`     | Liveness (empty 200)                                   |
| GET        | `/health`   | RAM, FS, Wi‑Fi RSSI, MCU temp, loop util %; uptime appears twice: `uptime_s` (scalar, cumulative seconds) and the `uptime` object (`total_s`, `since_boot_s`, `reboot_reason`, `scheduled_reboot`) |
| GET        | `/system`   | Board, MicroPython version, software version, Wi‑Fi IP/MAC |
| GET        | `/sensors`  | DHT reading; `?force=1` triggers on-demand measurement via `sensor.read()` (never raw `sensor.d.measure()`) |
| GET / POST | `/setpoints`| `{"temperature": int, "humidity": int}` — immediate hysteresis re-evaluation when control is enabled |
| GET / POST | `/outputs`  | `{"fan"?: bool, "humidifier"?: bool, "heater"?: bool}` — POST applies only keys present (≥1 required); responses always return all three |
| GET / POST | `/control`  | `{"enabled": bool}` — disabling calls `relays_off()` immediately (synchronous) |
| GET / POST | `/setup`    | GPIO pin mapping + `active_high`                          |
| POST       | `/provision`| Wi-Fi credential provisioning — intended for AP mode but **not mode-guarded**: accepted in STA mode too (writes config.json + reboots) |
| POST       | `/reboot`   | `{"type": "soft"\|"hard"}` (optional, default `soft`) — graceful reboot (soft = `machine.soft_reset()`, hard = `machine.reset()`); response returns before reboot. |

## Shared State Rules

- `app/state.py` exports mutable dictionaries: `status`, `setpoints`, `devices`.
- **Always mutate in place** — never reassign these module-level names.
- `_system_info` is cached (built once, read many times).
- **Relay-state keys must match exactly**: `status` keys (`fan`, `humidifier`, `heater`) must match `devices["pins"]` and the JSON names in `GET /` (`outputs.*`) and `GET /outputs`. The `IO` helpers (`hum_on()`, `fan_on()`, `heat_on()`, …) are the **only** writers of these keys, with one sanctioned exception: `POST /outputs` (`_setoutputs` in `app/api.py`), which is the documented manual-override path — it drives GPIO via the generic `io.write(pin, bool)` helper, then sets `status[...]` itself and calls `mark_relay_on()`/`mark_relay_off()` (from `app/control.py`) so min-runtime tracking stays consistent. Any *other* code writing these keys outside an `IO` helper is a bug. A key typo (e.g. `heat` vs `heater`) silently desyncs: the GPIO toggles but `status` stays stale, so the server always polls `false`.
- **No GPIO read-back**: relay state in the API responses comes from the cached `status` dict, not from live GPIO reads. The dict keys are the single source of truth — keep them correct.
- **Never snapshot `status[...]` into module-level dicts at import time** — always read from `status` live inside handlers. Module-level snapshots capture initial values and never update.

## Coding Rules

- **No type hints** — MicroPython support is limited; CPython type annotations are stripped before upload.
- **Never use blocking calls** inside async tasks; use `await asyncio.sleep_ms()`.
- **WiFi runtime operations must be async**: the blocking `connect_wifi()` is boot-only and must never be called from a running task. Runtime reconnect/monitoring uses `reconnect_once_async()` and `wifi_watchdog_loop()`, which poll with `await asyncio.sleep_ms()` so the control loop and server keep running.
- **Module ownership**: `app/wifi.py` owns all WiFi state transitions (connect, reconnect, link monitoring). `app/control.py` owns sensor sampling + hysteresis only. No cross-module WiFi logic in control.py.
- **HTTP handlers must not perform blocking sensor reads** (e.g. `sensor.d.measure()`) — use the last cached `status` values from `app/state.py` instead. The one exception is `GET /sensors?force=1`, which triggers an on-demand measurement via `sensor.read()`.
- **All POST handler error responses use the shape `{"ok": False, "error": "..."}`**. Do not mix flat `{"error": "..."}` with `{"ok": False, ...}` — standardise on `{"ok": False, "error": "..."}` across `/setpoints`, `/outputs`, `/control`, `/setup`, `/provision`, and `/reboot`.
- **All I/O inside the event loop is async** — use `asyncio.create_task()` for background work. Sole exception: the `main.py` boot path (module level, before `asyncio.run()` starts the loop), where blocking calls are acceptable because nothing else is scheduled yet — this is where the blocking `connect_wifi()` runs.
- **Error handling**: bare `except:` is acceptable; always log errors with `print()`.
- **Graceful shutdown**: use `stop_event` from `app/shutdown.py` — loops should check it and exit cleanly. The `finally` block in `main.py` calls `graceful_shutdown()` which turns all outputs off and disconnects WiFi (the `sensor` parameter is accepted but unused — it does not deinit the sensor).

## API Specification

No automatic spec generation (MicroPython). `spec/openapi.yaml` is hand-maintained, committed OpenAPI 3.0 — edit it directly; there is no Bruno collection. The mock (`mushpi-mock`) reads it as canonical — keep it current. Out-of-band payloads (e.g. the boot announce, POSTed to the hub, not a served path) go under `components.schemas`, never `paths:`; the announce payload must stay aligned with `mushpi-mock` and the server's `AnnouncePicoUnitDto`.

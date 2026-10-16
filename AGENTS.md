# mushpi-grow — Pico Growing Unit

MicroPython firmware for Raspberry Pi Pico 2W. Controls humidity and temperature via hysteresis, serves a REST API on `api_port` (default 5000), and announces itself to a central hub on boot.

> **Reference**: long-tail gotchas live in [`REFERENCE.md`](./REFERENCE.md) — its top lists the topics. Load it **only when the task touches those areas** — not on every spawn.

## Build, Lint, Test

There are no traditional build or test commands — this is plain MicroPython uploaded directly to the Pico:

- Deploy via the **MicroPico** VSCode extension (`paulober.pico-w-go`) — upload `.py`, `.html`, `.js` and `config.json` to flash (authoritative list: `micropico.syncFileTypes` in `.vscode/settings.json`); see README §Setup.
- Required third-party libs go in `/lib/` on the device: `microdot.py` (present in `lib/` locally but gitignored — not tracked in git). The announce uses the standard socket stack only — no HTTP client library is required.
- Version constants live in `app/state.py`: `_SOFTWARE_VERSION` (release-only — keep `spec/openapi.yaml` and the mock in sync) and `_API_VERSION` (bump only on breaking changes). Bump policy → REFERENCE §Version constants.

## Project Structure

```
mushpi-grow/
├── main.py              # Entry point — wires everything together; boot-path hub resolve; WDT armed before tasks
├── config.json          # WiFi, hub URL, device name, control params (gitignored)
├── README.md           # Setup + deployment instructions
├── HARDWARE.md         # Canonical BOM, pin map, wiring, power (maintained by mushpi-electronics)
├── LICENSE             # GPL-3.0 (verbatim FSF text)
├── .vscode/ + .micropico # MicroPico deploy tooling (tracked)
├── app/
│   ├── state.py         # Shared mutable dicts (status, setpoints, devices)
│   ├── hw.py            # IO class — GPIO init, relay control, LED heartbeat
│   ├── sensor.py        # DHTReader — reads DHT11, updates status
│   ├── control.py       # control_loop() — hysteresis-based async coroutine; the only watchdog feed site (fed via `wdt=`)
│   ├── announce.py      # announce_then_retry_once() — boot-only POST to the hub; retry/backoff → REFERENCE §Announce transport
│   ├── api.py           # Microdot REST API (port `api_port`)
│   ├── metrics.py       # system_snapshot() — RAM, FS, Wi‑Fi RSSI, MCU temp, uptime, loop util
│   ├── wifi.py          # connect_wifi() (blocking boot connect), reconnect_once_async() (async reconnect), wifi_watchdog_loop() (link monitor with backoff; no announce responsibility → REFERENCE §Wi-Fi watchdog)
│   ├── config_loader.py # load_config() + save_config() — atomic config read/write
│   ├── config_validator.py # validate_config() — boot-time config field validation
│   ├── uptime.py         # Cumulative uptime tracking, boot-reason classification, uptime.json persistence
│   ├── runtime_state.py # runtime.json persistence — setpoints, loop flag, hand-set fan/humidifier; restore() at boot → REFERENCE §Persistence
│   ├── reboot_scheduler.py # Daily scheduled soft-reboot (STA mode), NTP sync
│   ├── provision.html   # HTML form served in AP provisioning mode
│   ├── unit.html        # Status/control page served at GET /ui (MODE/DEVNAME bootstrap; page logic in static/unit.js) → REFERENCE §Unit page (mock-vendored — keep the mirror current)
│   ├── static/          # Unit page static assets (unit.js, favicon.svg) served at GET /static/<filename>, streamed → REFERENCE §Unit page (mock-vendored)
│   └── shutdown.py      # graceful_shutdown() + reboot() + soft_reboot()
├── spec/
│   └── openapi.yaml     # Hand-maintained OpenAPI 3.0 spec (committed)
└── lib/
    └── microdot.py      # Microdot async web framework (gitignored — upload to device /lib/); no module imports urequests any more
```

## REST API (`api_port`, default 5000)

| Method     | Path        | Notes                                                  |
|------------|-------------|--------------------------------------------------------|
| GET        | `/`         | STA: full JSON snapshot (incl. additive `hub` ack state); a browser `Accept` (text/html without application/json) gets the unit page instead. AP: HTML provisioning form. CORS on all responses. |
| GET        | `/ui`       | On-device status & control page — HTML in both modes; AP serves a view-only provisioning variant → REFERENCE §Unit page |
| GET        | `/static/<filename>` | Page assets `unit.js` (application/javascript), `favicon.svg` (image/svg+xml) — streamed in 1 KB chunks, Cache-Control: max-age=3600; 404 `{"ok": false, "error": "static asset missing"}` if absent; not consumed by hub or server → REFERENCE §Unit page |
| GET        | `/ping`     | Liveness (empty 200)                                   |
| GET        | `/health`   | RAM, FS, Wi‑Fi RSSI, MCU temp, loop util %; uptime twice — `uptime_s` plus the `uptime` object |
| GET        | `/system`   | Board, MicroPython version, software version, Wi‑Fi IP/MAC |
| GET        | `/sensors`  | DHT reading; `?force=1` triggers one on-demand measurement |
| GET / POST | `/setpoints`| `{"temperature": int, "humidity": int}` — immediate hysteresis re-evaluation when control is enabled. POST persists to flash (`runtime.json`) — survives reboot. AP: POST 403 (view-only) |
| GET / POST | `/outputs`  | `{"fan"?: bool, "humidifier"?: bool, "heater"?: bool}` — POST applies only keys present (≥1 required); responses always return all three. POST persists the applied fan/humidifier to flash (`runtime.json`) — restored on reboot when the loop is off; **`heater` never persists**. AP: POST 403 (view-only) |
| GET / POST | `/control`  | `{"enabled": bool}` — disabling calls `relays_off()` immediately (synchronous). POST persists the flag to flash (`runtime.json`) — the loop restarts in the state it was left in. AP: POST 403 (view-only) |
| GET / POST | `/setup`    | GPIO pin mapping + `active_high` — assignable GPIOs 1–22, 26, 27, 28 only. AP: POST 403 (view-only) |
| POST       | `/provision`| Wi-Fi credential provisioning — **mode-guarded to AP mode**: in STA mode rejected with 403 `{"ok": false, "error": "..."}` before any config write or reboot; in AP mode writes config.json + reboots |
| POST       | `/reboot`   | `{"type": "soft"\|"hard"}` (optional, default `soft`) — graceful reboot (soft = `machine.soft_reset()`, hard = `machine.reset()`); response returns before reboot. |
| OPTIONS    | any         | CORS preflight (`app.options_handler`) — not contract, absent from the spec → REFERENCE §CORS preflight |

## Shared State Rules

- `app/state.py` exports mutable dictionaries: `status`, `setpoints`, `devices`.
- **Always mutate in place** — never reassign these module-level names.
- `_system_info` is cached (built once, read many times).
- **Relay-state keys must match exactly**: `status` keys (`fan`, `humidifier`, `heater`) mirror `devices["pins"]` and the JSON outputs. The `IO` helpers (`hum_on()`, `fan_on()`, `heat_on()`, …) are the **only** writers, plus one sanctioned exception: `POST /outputs` (`_setoutputs`). Boot restore (`runtime_state.restore()`) is sanctioned too but drives relays **only through the `IO` helpers**, so it is not a direct `status` writer. Any other writer is a bug → REFERENCE §Shared-state pitfalls.
- **Durability writers are POST-only**: only the three `api.py` POST handlers (`/setpoints`, `/outputs`, `/control`) write `runtime.json`, and only `main.py` calls `runtime_state.restore()` (boot slot). `app/control.py`, `app/hw.py` and `app/sensor.py` must **never** import `app/runtime_state.py` — loop-driven switching must never touch flash.
- **No GPIO read-back**: API relay state comes from the cached `status` dict, never live GPIO reads — those keys are the single source of truth.
- **Never snapshot `status[...]` into module-level dicts at import time** — read from `status` live inside handlers; a snapshot captures initial values and never updates.

## Coding Rules

- **No type hints** — MicroPython support is limited; CPython type annotations are stripped before upload.
- **Never use blocking calls** inside async tasks; use `await asyncio.sleep_ms()`.
- **The hardware watchdog is fed from exactly one place**: `control_loop()`'s chunked-sleep loop, via the `wdt` armed in `main()` before any task is created. Never feed it elsewhere — a dead control loop must stop being fed. Budget rules → REFERENCE §Announce transport & watchdog budgets.
- **WiFi runtime operations must be async**: `connect_wifi()` is boot-only and must never be called from a running task; runtime reconnect/monitoring is `reconnect_once_async()` / `wifi_watchdog_loop()` (REFERENCE §Wi-Fi watchdog).
- **Module ownership**: `app/wifi.py` owns all WiFi state transitions (connect, reconnect, link monitoring). `app/control.py` owns sensor sampling + hysteresis only. No cross-module WiFi logic in control.py.
- **HTTP handlers must not perform blocking sensor reads** (e.g. `sensor.d.measure()`) — use the cached `status` values instead. The one exception is `GET /sensors?force=1`, which triggers an on-demand measurement via `sensor.read()`.
- **All POST handler error responses use the shape `{"ok": False, "error": "..."}`**. Do not mix flat `{"error": "..."}` with `{"ok": False, ...}` — standardise on `{"ok": False, "error": "..."}` across `/setpoints`, `/outputs`, `/control`, `/setup`, `/provision`, and `/reboot`.
- **All I/O inside the event loop is async** — use `asyncio.create_task()` for background work. Sole exception: the `main.py` boot path (before `asyncio.run()` starts the loop), where blocking is acceptable because nothing else is scheduled yet — this is where the blocking `connect_wifi()` runs.
- **Error handling**: bare `except:` is acceptable; always log errors with `print()`.
- **Graceful shutdown**: use `stop_event` from `app/shutdown.py` — loops should check it and exit cleanly. `main.py`'s `finally` calls `graceful_shutdown()`, which turns all outputs off and disconnects WiFi (its `sensor` parameter is accepted but unused — the sensor is not deinit'd).

## API Specification

No automatic spec generation (MicroPython). `spec/openapi.yaml` is hand-maintained, committed OpenAPI 3.0 — edit it directly; there is no Bruno collection. `mushpi-mock` reads it as canonical — keep it current. Out-of-band payloads (the boot announce, POSTed to the hub, not a served path) go under `components.schemas`, never `paths:`; the announce payload must stay aligned with `mushpi-mock` and the server's `AnnouncePicoUnitDto`.

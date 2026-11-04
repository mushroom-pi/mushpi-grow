# mushpi-grow — Pico Growing Unit

MicroPython firmware for Raspberry Pi Pico 2W. Controls humidity and temperature via hysteresis, serves a REST API on `api_port` (default 5000), and announces itself to a central hub on boot.

> **Reference**: long-tail gotchas live in [`REFERENCE.md`](./REFERENCE.md) (its top lists the topics). Load it **only when the task touches those areas**.

## Build, Lint, Test

No traditional build or test commands — plain MicroPython uploaded directly to the Pico:

- Deploy via the **MicroPico** VSCode extension (`paulober.pico-w-go`) — upload `.py`, `.html`, `.js` and `config.json` to flash (authoritative list: `micropico.syncFileTypes` in `.vscode/settings.json`); see README §Setup.
- Required third-party libs go in `/lib/` on the device: `microdot.py` (gitignored locally). The announce needs no HTTP client library (socket stack only → REFERENCE §Announce transport & watchdog budgets).
- Version constants live in `app/state.py` (`_SOFTWARE_VERSION`, `_API_VERSION`) — bump policy → REFERENCE §Version constants.

## Project Structure

```
mushpi-grow/
├── main.py              # Entry point — wires everything together; boot-path hub resolve; WDT armed once in main(), control loop spawned per branch (STA / offline AP)
├── config.json          # WiFi, hub URL, device name, control params (gitignored)
├── README.md           # Setup + deployment instructions
├── HARDWARE.md         # Canonical BOM, pin map, wiring, power (maintained by mushpi-electronics)
├── LICENSE             # GPL-3.0 (verbatim FSF text)
├── .vscode/ + .micropico # MicroPico deploy tooling (tracked)
├── app/
│   ├── state.py         # Shared mutable dicts (status, setpoints, devices)
│   ├── hw.py            # IO class — GPIO init, relay control, LED heartbeat
│   ├── sensor.py        # DHTReader — reads DHT11, updates status
│   ├── control.py       # control_loop() — hysteresis-based async coroutine; the only watchdog feed site (fed via `wdt=`); offline sensor-fault output cut → REFERENCE §Control Loop Logic
│   ├── announce.py      # announce_then_retry_once() — boot-only POST to the hub; retry/backoff → REFERENCE §Announce transport
│   ├── api.py           # Microdot REST API (port `api_port`)
│   ├── metrics.py       # system_snapshot() — RAM, FS, Wi‑Fi RSSI, MCU temp, uptime, loop util
│   ├── wifi.py          # connect_wifi() (blocking boot connect), reconnect_once_async() (async reconnect), ensure_ap_up()/ap_down() (AP lifecycle), wifi_watchdog_loop() (link monitor → REFERENCE §Wi-Fi watchdog)
│   ├── config_loader.py # load_config() + save_config() — atomic config read/write
│   ├── config_validator.py # validate_config() — boot-time config field validation
│   ├── uptime.py         # Cumulative uptime tracking, boot-reason classification, uptime.json persistence
│   ├── runtime_state.py # runtime.json persistence — setpoints, loop flag, hand-set fan/humidifier; restore() at boot → REFERENCE §Persistence
│   ├── reboot_scheduler.py # Daily scheduled soft-reboot (STA mode), NTP sync
│   ├── provision.html   # Wi-Fi setup form served at GET /provision (both modes), submitted by POST /provision
│   ├── unit.html        # Status/control page served at GET / (HTML browsers, both modes) and GET /ui (MODE/DEVNAME bootstrap; page logic in static/unit.js) → REFERENCE §Unit page (mock-vendored — keep the mirror current)
│   ├── static/          # Unit page static assets (unit.js, favicon.svg) served at GET /static/<filename>, streamed → REFERENCE §Unit page (mock-vendored)
│   └── shutdown.py      # graceful_shutdown() + reboot() + soft_reboot()
├── spec/
│   └── openapi.yaml     # Hand-maintained OpenAPI 3.0 spec (committed)
└── lib/
    └── microdot.py      # Microdot async web framework (gitignored — upload to device /lib/)
```

## REST API (`api_port`, default 5000)

| Method     | Path        | Notes                                                  |
|------------|-------------|--------------------------------------------------------|
| GET        | `/`         | Landing page in both modes: full JSON snapshot (incl. additive `hub` ack state); a browser `Accept` (html, no `application/json`) gets the unit page instead — offline it still regulates and the page shows the offline banner. No AP special case. CORS on all responses. |
| GET        | `/ui`       | Status & control page — alias of `/` for HTML browsers; HTML in both modes; the offline (AP) variant is fully operable, pin remapping included → REFERENCE §Offline (AP) operating mode |
| GET        | `/static/<filename>` | Page assets `unit.js`, `favicon.svg` — streamed, 404 if absent; not consumed by hub or server → REFERENCE §Unit page |
| GET        | `/ping`     | Liveness (empty 200)                                   |
| GET        | `/health`   | RAM, FS, Wi‑Fi RSSI, MCU temp, loop util %; uptime twice — `uptime_s` plus the `uptime` object |
| GET        | `/system`   | Board, MicroPython version, software version, Wi‑Fi IP/MAC |
| GET        | `/sensors`  | DHT reading; `?force=1` triggers one on-demand measurement |
| GET / POST | `/setpoints`| `{"temperature": int, "humidity": int}` — immediate hysteresis re-evaluation when control is enabled (skipped while the offline sensor-fault cut is latched). POST persists to flash (`runtime.json`). offline/AP: accepted, persists |
| GET / POST | `/outputs`  | `{"fan"?: bool, "humidifier"?: bool, "heater"?: bool}` — POST applies only keys present (≥1 required); responses always return all three. POST persists the applied fan/humidifier to flash (`runtime.json`); **`heater` never persists**. offline/AP: accepted, persists |
| GET / POST | `/control`  | `{"enabled": bool}` — disabling calls `relays_off()` immediately (synchronous). POST persists the flag to flash (`runtime.json`). offline/AP: accepted, persists |
| GET / POST | `/setup`    | GPIO pin mapping + `active_high` — assignable GPIOs 1–22, 26, 27, 28 only. offline/AP: accepted — the merged map is re-checked against the boot duplicate rule before writing |
| GET / POST | `/provision`| GET: Wi-Fi setup form (HTML, both modes) — the offline banner's link target. POST: Wi-Fi credential provisioning — **mode-guarded to the offline (AP) operating mode**: on a connected (STA) unit rejected with 403 before any config write or reboot; offline writes config.json + reboots |
| POST       | `/reboot`   | `{"type": "soft"\|"hard"}` (optional, default `soft`) — graceful reboot (soft = `machine.soft_reset()`, hard = `machine.reset()`); response returns before reboot. |
| OPTIONS    | any         | CORS preflight (`app.options_handler`) — not contract, absent from the spec → REFERENCE §CORS preflight |

## Shared State Rules

- `app/state.py` exports mutable dictionaries `status`, `setpoints`, `devices` — **always mutate in place**, never reassign these module-level names.
- `_system_info` is cached (built once, read many times).
- **Relay-state keys must match exactly**: `status` keys (`fan`, `humidifier`, `heater`) mirror `devices["pins"]` and the JSON outputs. The `IO` helpers (`hum_on()`, `fan_on()`, `heat_on()`, …) are the **only** writers, plus one sanctioned exception: `POST /outputs` (`_setoutputs`). Any other writer is a bug → REFERENCE §Shared-state pitfalls.
- **Durability writers are POST-only**: only the three `api.py` POST handlers (`/setpoints`, `/outputs`, `/control`) write `runtime.json`, and only `main.py` calls `runtime_state.restore()` (boot slot). `app/control.py`, `app/hw.py` and `app/sensor.py` must **never** import `app/runtime_state.py`.
- **No GPIO read-back**: API relay state comes from the cached `status` dict, never live GPIO reads.
- **Never snapshot `status[...]` into module-level dicts at import time** — read from `status` live inside handlers.

## Coding Rules

- **No type hints** (→ REFERENCE §MicroPython quirks).
- **Never use blocking calls** inside async tasks; use `await asyncio.sleep_ms()`.
- **The hardware watchdog is fed from exactly one place**: `control_loop()`'s chunked-sleep loop, via the `wdt` armed in `main()` before any task is created. Never feed it elsewhere. Budget rules → REFERENCE §Announce transport & watchdog budgets.
- **WiFi runtime operations must be async**: `connect_wifi()` is boot-only; runtime reconnect/monitoring is `reconnect_once_async()` / `wifi_watchdog_loop()` (REFERENCE §Wi-Fi watchdog).
- **Module ownership**: `app/wifi.py` owns all WiFi state transitions (connect, reconnect, link monitoring). `app/control.py` owns sensor sampling + hysteresis only.
- **HTTP handlers must not perform blocking sensor reads** (e.g. `sensor.d.measure()`) — use the cached `status` values instead. The one exception is `GET /sensors?force=1` (via `sensor.read()`).
- **All POST handler error responses use the shape `{"ok": False, "error": "..."}`** — never a flat `{"error": "..."}`.
- **All I/O inside the event loop is async** — use `asyncio.create_task()` for background work. Sole exception: the `main.py` boot path (before `asyncio.run()` starts the loop — rationale → REFERENCE §Announce transport & watchdog budgets).
- **Error handling**: bare `except:` is acceptable; always log errors with `print()`.
- **Graceful shutdown**: use `stop_event` from `app/shutdown.py` — loops should check it and exit cleanly. `main.py`'s `finally` calls `graceful_shutdown()` (outputs off, WiFi disconnect → REFERENCE §Graceful shutdown).

## API Specification

No automatic spec generation (MicroPython). `spec/openapi.yaml` is hand-maintained, committed OpenAPI 3.0 — edit it directly; there is no Bruno collection. `mushpi-mock` reads it as canonical — keep it current. Out-of-band payloads (the boot announce, not a served path) go under `components.schemas`, never `paths:`; keep the announce payload aligned with `mushpi-mock` and the server's `AnnouncePicoUnitDto`.

# mushpi-grow — Pico Growing Unit

MicroPython firmware for Raspberry Pi Pico 2W. Controls humidity and temperature via hysteresis, serves a REST API on port 5000, and announces itself to a central hub on boot. See also `.github/copilot-instructions.md` for the original Copilot agent instructions.

## Build, Lint, Test

There are no traditional build or test commands — this is plain MicroPython uploaded directly to the Pico. To deploy:

- Use the **MicroPico** VSCode extension (`paulober.pico-w-go` in `.vscode/extensions.json`).
- Upload all `.py` files, `.html` files (e.g. `app/provision.html`), and `config.json` to the Pico's flash.
- Required third-party libs go in `/lib/` on the device: `microdot.py`, `urequests.py` (already vendored in the repo).
- Bump `_SOFTWARE_VERSION` in `app/state.py` on every release. It is the single source of truth for the firmware version (returned by `GET /system` and `GET /`).

## Project Structure

```
mushpi-grow/
├── main.py              # Entry point — wires everything together
├── config.json          # WiFi, hub URL, device name, control params (gitignored)
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
│   └── openapi.yaml     # Hand-maintained OpenAPI 3.0 spec (exported from Bruno)
└── lib/
    ├── microdot.py      # Microdot async web framework (vendored)
    └── urequests.py     # MicroPython HTTP client (vendored)
```

## Startup Sequence (`main.py`)

1. `IO()` — initialises GPIO, turns onboard LED ON.
2. `DHTReader()` — initialises DHT11 sensor.
3. `load_config()` — reads `config.json` with shallow merge over defaults. Returns `(cfg, load_errors)` — catches malformed JSON and non-object top-level gracefully.
4. `validate_config(cfg)` — validates all fields (device_name, api_port, hub_url, wifi, control) for type and range correctness. Accumulates all errors. If any errors (load or validation): prints them to serial, then enters terminal `error_blink_blocking()` state (3 fast blinks + pause forever). Device does NOT start WiFi, control loop, or server.
5. **Apply persisted GPIO mapping** — reads `pins` and `active_high` from `cfg`, applies them to `state.devices`, then calls `sensor.__init__()` and `io.__init__()` to re-initialise GPIO on the persisted pin assignments and polarity. Runs after validation so bad persisted data can't crash re-init; the prior default-constructed `io`/`sensor` instances are briefly double-inited (harmless — relays are OFF).
6. `init_uptime()` — reads `uptime.json` sentinel first: if `pending_type` is set ("scheduled"/"soft"/"hard"), that's the reboot reason (preserving cumulative for scheduled/soft, resetting for hard). Falls back to `machine.reset_cause()` only when no sentinel exists (unexpected reboots: WDT → "watchdog", PWRON → "power_on"). Writes fresh `uptime.json` to flash.
7. `state.init_system_info(cfg)` — caches board/MicroPython/build metadata.
8. `state.check_mdns_firmware(cfg["device_name"])` — warns if firmware < v1.26.0 (mDNS won't work; see README).
9. **Force-provision check** — reads GP0 (internal pull-up). If LOW, sets `_force_provision = True`.
10. `connect_wifi()` — tries each candidate network (primary + `networks` list), up to `retries` per SSID; LED stays OFF until connected, then LED ON. Returns `(wlan, ip)`.
11. **AP mode decision**: if `_force_provision` OR `ip is None`:
    - `start_ap_provisioning(cfg)` — open AP at `192.168.4.1`
    - `io.start_provisioning_blink()` — slow double-blink LED pattern
    - `main()` runs only `start_server(mode="ap")` — no announce, no control loop
12. **STA mode** (normal boot):
    - `state.attach_wlan_info(wlan, cfg)` — caches MAC, IP, hostname, port.
    - Initialises hardware WDT (`WDT(timeout=8000)` — 8-second timeout). Must be created **after** `connect_wifi()` returns (boot WiFi can take 30s+ and would trigger a spurious reboot).
    - `main()` async task:
      - `start_metrics()` — launches background event-loop utilisation meter.
      - Spawns `announce_then_retry_once()` — tries POST to hub; if it fails, waits 60 s and retries once.
      - Spawns `control_loop()` — hysteresis loop; also feeds the WDT every 200ms via its chunked-sleep loop. If the loop hangs or dies, the WDT expires → hard reboot → WiFi reconnects + re-announces.
      - Spawns `wifi_watchdog_loop()` — monitors link health, reconnects with exponential backoff on drop.
      - Spawns `reboot_scheduler_loop()` — syncs NTP after WiFi connect, then polls every 60s; triggers `machine.soft_reset()` at the configured hour/minute (disabled by default). Not spawned in AP mode.
      - `await start_server()` — Microdot HTTP server on `0.0.0.0:<api_port>`.

## Firmware requirements

For `<device_name>.local` mDNS resolution (so the hub or a browser can reach the unit at `http://<device_name>.local:5000` without knowing its IP), the Pico must run **MicroPython ≥ v1.26.0** (v1.25.0 stable also contains the fix, but v1.26.0 is the recommended minimum and confirmed working). Pre-release/preview builds of v1.25.0 do **not** have the fix. AP-mode mDNS is unsupported ([#10957](https://github.com/micropython/micropython/issues/10957)). mDNS is link-local — only resolves on the same subnet; cross-subnet needs a router mDNS reflector.

The rp2/CYW43 mDNS responder was half-wired for years: `mdns_resp_init()` opened UDP/5353 but the driver never called `mdns_resp_add_netif()`, so no announcement packets were sent. Fixed in v1.25.0 stable by [PR #17057](https://github.com/micropython/micropython/pull/17057). `app/wifi.py` uses `network.hostname(name)` before `active(True)` (the modern API; `wlan.config(hostname=...)` is deprecated). `state.check_mdns_firmware()` warns at boot if the firmware is too old.

## Hardware Pins (defaults)

| Device           | GPIO | Notes                                                       |
|------------------|------|-------------------------------------------------------------|
| Force-provision  | 0    | Internal pull-up; hold LOW at boot to force AP mode         |
| DHT11            | 4    | Temperature + humidity sensor                               |
| Humidifier relay | 6    | Active-low by default                                       |
| Fan relay        | 7    | Active-low by default                                       |
| Heater relay     | 8    | Active-low by default                                       |
| Onboard LED      | `LED`| Status indicator                                            |

- `active_high` in `config.json` flips relay polarity. The `IO` class in `app/hw.py` handles this automatically — never toggle GPIO directly.
- Pins can be remapped at runtime via `POST /setup` and are persisted to `config.json` (`pins`, `active_high` keys), surviving reboot.

## Control Loop Logic

Runs as a `uasyncio` coroutine every `control.period_s` seconds (default 10 s in copilot instructions, 30 s in the checked-in `config.json`, default 5 s in `config_loader.py`).

- **Humidifier**: ON when `humidity <= target − humidity_deadband` (asymmetric low-side deadband). Stays ON until humidity rises above the deadband threshold.
- **Fan**: ON when `humidity > target + hyst_hum` (high-side deadband). Stays ON until humidity drops to `<= target + hyst_hum`.
- **Deadband**: between `target − humidity_deadband` and `target + hyst_hum` neither humidifier nor fan runs — prevents fighting between the two actuators. The deadband is asymmetric: the low-side gap (`humidity_deadband`, default 3) and high-side gap (`hyst_hum`, default 5) are independently configurable.
- **Heater**: ON when `temperature <= target − temperature_deadband` (asymmetric low-side deadband, default 1).
- **Minimum runtime**: once a relay turns ON, it stays ON for at least `min_runtime` seconds (default 30) before it can be turned off. Tracked per-relay via `_on_since` timestamps (ticks_ms). This prevents rapid cycling of relays when readings hover near deadband boundaries.
- Humidifier and fan are **mutually exclusive** — each branch calls the other's `_turn_off()` helper as a safety measure.
- When `control_enabled = False`: keeps sampling the DHT11 every `control.period_s`, calls `relays_off()` once (relays only — LED heartbeat continues), resets `_on_since` tracking, and skips hysteresis actuation until re-enabled. Sampling is decoupled from actuation so `GET /`, `/sensors`, and `/health` keep returning fresh readings.
- API handlers (`POST /outputs`, `POST /control`) also update `_on_since` so min-runtime tracking stays consistent when relays are driven outside the control loop.

**Key design rule**: sensor sampling must remain independent of `control_enabled` (observability vs. actuation separation). `io.all_off()` is for fail-safe/shutdown only; use `io.relays_off()` for control-disabled state.

## REST API (port 5000)

| Method     | Path        | Notes                                                  |
|------------|-------------|--------------------------------------------------------|
| GET        | `/`         | STA: full JSON snapshot. AP: HTML provisioning form. CORS headers on all responses. |
| GET        | `/ping`     | Liveness (empty 200)                                   |
| GET        | `/health`   | RAM, FS, Wi‑Fi RSSI, MCU temp, uptime, loop util %     |
| GET        | `/system`   | Board, MicroPython version, software version, Wi‑Fi IP/MAC |
| GET / POST | `/sensors`  | DHT reading; `?force=1` triggers on-demand measurement |
| GET / POST | `/setpoints`| `{"temperature": int, "humidity": int}` — triggers immediate hysteresis re-evaluation when control is enabled |
| GET / POST | `/outputs`  | `{"fan": bool, "humidifier": bool, "heater": bool}`     |
| GET / POST | `/control`  | `{"enabled": bool}` — disables control immediately (calls `relays_off()` sync) |
| GET / POST | `/setup`    | GPIO pin mapping + `active_high`                          |
| POST       | `/provision`| Wi-Fi credential provisioning (AP mode only — writes config.json + reboots) |
| POST       | `/reboot`   | `{"type": "soft"\|"hard"}` — triggers graceful reboot (soft = `machine.soft_reset()`, hard = `machine.reset()`). Response returns before reboot executes. |

## API Specification

The Pico firmware has no automatic spec generation (MicroPython, 264 KB RAM). Instead, the REST API is documented manually:

- **`spec/openapi.yaml`**: a hand-maintained OpenAPI 3.0 spec describing all Pico endpoints. Committed to the repo.
- **Workflow**: the Bruno collection is the source of truth. When the API changes, update the Bruno collection first, then export it as `spec/openapi.yaml` and commit. This keeps the YAML spec reviewable in PRs alongside the code changes.
- The spec serves as both human-readable documentation and an import source for API clients/tools (e.g. Bruno, Swagger Editor).

### AP Provisioning Mode

When the Pico fails to connect to Wi-Fi after 3 retries (or GP0 is held LOW at boot), it enters AP provisioning mode:
- SSID: `mushpi-provision-XXXX` (XXXX = last 4 hex of AP MAC), open network
- IP: `192.168.4.1`, port `5000`
- LED: slow double-blink (200/200/200/800ms)
- `GET /` returns an HTML setup form
- `POST /provision` with `{"wifi":{"ssid":"...","password":"..."}}` writes `config.json` and reboots
- CORS headers are enabled on all responses
- mDNS does NOT resolve in AP mode — use the IP directly
- Relays remain OFF during provisioning (control loop is not started)
- Force-provision: hold GP0 to GND during boot (internal pull-up)

## config.json

```json
{
  "wifi": {
    "ssid": "MyNetwork",
    "password": "secret",
    "networks": [
      {"ssid": "FallbackNetwork", "password": "secret2"}
    ]
  },
  "hub_url": "http://<hub_ip>:3000/pico-units/announce",
  "hub_secret": "mushpi-dev-secret",
  "device_name": "pico-unit1",
  "api_port": 5000,
  "control": { "period_s": 10, "hyst_hum": 5, "hyst_temp": 1, "humidity_deadband": 3, "temperature_deadband": 1, "min_runtime": 30 },
  "pins": { "dht": 4, "humidifier": 6, "fan": 7, "heater": 8 },
  "active_high": false
}
```

- `device_name` must match the `handle` field in `mushpi-server`'s `PicoUnit` entity.
- `hub_secret` must match the server's `PICO_ANNOUNCE_SECRET` env var. Sent as `X-Pico-Secret` header on every announcement POST.
- `config.json` is gitignored — a checked-in copy with real credentials exists locally.
- `config_loader.py` provides defaults for all fields (device_name: `"PicoDevice"`, period_s: `5`).
- `networks` is optional. If present, `connect_wifi()` tries each in order; first success wins. The primary `ssid`/`password` pair is tried first.
- `pins` is a dict mapping device roles to GPIO pin numbers (`dht`, `humidifier`, `fan`, `heater`). Each value must be an integer in 1–29 (GP0 is reserved for force-provision). Persisted by `POST /setup`; falls back to defaults if missing.
- `active_high` is a boolean controlling relay polarity (`false` = active-low, the safe default for common relay modules). Persisted by `POST /setup`; falls back to `false` if missing.

## LED Status Indicators

- **OFF**: WiFi connection failed — check SSID/password.
- **SOLID ON**: WiFi connected but hub announcement failed (or still in progress).
- **BLINKING** (heartbeat): WiFi connected and hub announcement succeeded.
- **PROVISIONING** (slow double-blink): 200ms on / 200ms off / 200ms on / 800ms off — AP mode active, awaiting Wi-Fi credentials via web form.
- **CONFIG ERROR** (3 fast blinks): 150ms on / 150ms off × 3, 1000ms pause, repeating — `config.json` is malformed or has invalid values; check serial console for details. Terminal state — the device does not start any services.

## Shared State Rules

- `app/state.py` exports mutable dictionaries: `status`, `setpoints`, `devices`.
- **Always mutate in place** — never reassign these module-level names.
- `_system_info` is cached (built once, read many times).
- `_SOFTWARE_VERSION` in `state.py` is the single source of truth for the firmware version. Bump it on every release.
- **Relay-state keys must match exactly**: the keys in `status` (`fan`, `humidifier`, `heater`) must match the keys in `devices["pins"]` and the JSON field names returned by `GET /` (`outputs.fan`, etc.) and `GET /outputs`. The `IO` helper methods (`hum_on()`, `fan_on()`, `heat_on()`, etc.) are the **only** code allowed to write these keys. A typo in the key name (e.g. `status["heat"]` instead of `status["heater"]`) causes a silent desync: the GPIO pin toggles correctly but the status dict value stays stale, and the server will always poll `false`.
- **No GPIO read-back**: relay state in the API responses comes from the cached `status` dict, not from live GPIO reads. The dict keys are the single source of truth — keep them correct.
- **Never snapshot `status[...]` into module-level dicts at import time** — always read from `status` live inside handlers. Module-level snapshots capture initial values and never update.

## Coding Rules

- **No type hints** — MicroPython support is limited; CPython type annotations are stripped before upload.
- **Never use blocking calls** inside async tasks; use `await asyncio.sleep_ms()`.
- **WiFi runtime operations must be async**: the blocking `connect_wifi()` is boot-only and must never be called from a running task. Runtime reconnect/monitoring uses `reconnect_once_async()` and `wifi_watchdog_loop()`, which poll with `await asyncio.sleep_ms()` so the control loop and server keep running.
- **Module ownership**: `app/wifi.py` owns all WiFi state transitions (connect, reconnect, link monitoring). `app/control.py` owns sensor sampling + hysteresis only. No cross-module WiFi logic in control.py.
- **WDT (Watchdog Timer)**: STA mode only (not AP provisioning). Initialised **after** `connect_wifi()` returns (boot WiFi can take 30s+ and would trigger a spurious reboot). Fed from `control_loop`'s 200ms chunked-sleep loop — covers event-loop-wide blocking hangs and control_loop task death. Not fed from an independent task (would miss control_loop death).
- **`announce_then_retry_once` is re-entrant**: it is safe to call at runtime (not just boot). The caller must cancel any prior announce task before spawning a new one to avoid overlapping heartbeat/LED control.
- **HTTP handlers must not perform blocking sensor reads** (e.g. `sensor.d.measure()`) — use the last cached `status` values from `app/state.py` instead. The one exception is `GET /sensors?force=1`, which triggers an on-demand measurement intentionally.
- **All I/O is async** — use `asyncio.create_task()` for background work.
- **Error handling**: bare `except:` is acceptable; always log errors with `print()`.
- **RAM budget**: ~264 KB on RP2040; avoid large imports, f-strings, or unnecessary allocations.
- **Required libs** (upload to `/lib/` on the Pico): `microdot.py`, `urequests.py`.
- **Graceful shutdown**: use `stop_event` from `app/shutdown.py` — loops should check it and exit cleanly. The `finally` block in `main.py` calls `graceful_shutdown()` which turns all outputs off, disconnects WiFi, and deinits the sensor.
- **MicroPython quirks**:
  - `urequests` does not support the `json=` kwarg — pass pre-serialised `ujson.dumps(body)` as `data=` with explicit `Content-Type: application/json` header.
  - `usocket.setdefaulttimeout()` is used to set per-request timeouts and restored to `None` afterwards.
  - DHT11 can return implausible readings; `DHTReader.plausible()` validates `-10 ≤ t ≤ 60` and `0 ≤ h ≤ 100`.
  - **Sentinel-based reboot classification**: `machine.reset_cause()` on rp2 is unreliable for distinguishing intentional from watchdog reboots (both `machine.reset()` and `machine.soft_reset()` may report as `WDT_RESET`). Instead, **all intentional reboots** (scheduled, `POST /reboot`) write a `pending_type` sentinel to `uptime.json` via `mark_pending_reboot()` before resetting. `init_uptime()` reads the sentinel first; `reset_cause()` is used only as a fallback for unexpected reboots.
  - **No battery-backed RTC**: `time.localtime()` is garbage until NTP-synced. Any wall-clock feature must NTP-sync first (blocking, in the STA boot path — never from an async task). NTP-sync logic lives in `app/reboot_scheduler.py`.
- **`uptime.json`** is a runtime-persisted file at flash root (like `config.json`). Created by `init_uptime()` on every boot. Schema: `{"cumulative_ms": int, "reboot_done_date": "YYYY-MM-DD"}`. Device-local only — never sent to the hub. Atomic write via `.tmp` + `os.rename`.
- **Persist config** with `save_config(cfg)` — atomic write via `config.json.tmp` + `os.rename`. Never `open('config.json','w')` directly.
- **`POST /provision` `wifi` key** persists to flash. **`POST /setup`** also persists `pins` and `active_high` to flash (survives reboot). Setpoints (via `POST /setpoints`) remain runtime-only (lost on reboot).

# mushpi-grow — Reference (On-Demand)

Long-tail details and gotchas. **Load only when the task touches these areas** — do not read on every spawn. The always-loaded [`AGENTS.md`](./AGENTS.md) holds the module map, deploy steps, REST API table, shared-state rules, and core coding rules.

Topics covered here: startup sequence · firmware/mDNS requirements · hardware pins · control-loop algorithm · `config.json` schema · AP provisioning · LED states · Wi-Fi watchdog & re-announce · MicroPython quirks · RAM/gc · persistence.

---

## Startup Sequence (`main.py`)

1. `IO()` — initialises GPIO, turns onboard LED ON.
2. `DHTReader()` — initialises DHT11 sensor.
3. `load_config()` — reads `config.json` with shallow merge over defaults. Returns `(cfg, load_errors)` — catches malformed JSON and non-object top-level gracefully.
4. `validate_config(cfg)` — validates all fields (device_name, api_port, hub_url, wifi, control) for type and range correctness. Accumulates all errors. If any errors (load or validation): prints them to serial, then enters terminal `error_blink_blocking()` state (3 fast blinks + pause forever). Device does NOT start WiFi, control loop, or server.
5. **Apply persisted GPIO mapping** — reads `pins` and `active_high` from `cfg`, applies them to `state.devices`, then calls `sensor.__init__()` and `io.__init__()` to re-initialise GPIO on the persisted pin assignments and polarity. Runs after validation so bad persisted data can't crash re-init; the prior default-constructed `io`/`sensor` instances are briefly double-inited (harmless — relays are OFF).
6. `init_uptime()` — reads `uptime.json` sentinel first: if `pending_type` is set ("scheduled"/"soft"/"hard"), that's the reboot reason (preserving cumulative for scheduled/soft, resetting for hard). Falls back to `machine.reset_cause()` only when no sentinel exists (unexpected reboots: WDT → "watchdog", PWRON → "power_on"). Writes fresh `uptime.json` to flash.
7. `state.init_system_info(cfg)` — caches board/MicroPython/build metadata.
8. `state.check_mdns_firmware(cfg["device_name"])` — warns if firmware < v1.26.0 (mDNS won't work; see README).
9. **Force-provision check** — reads GP0 (internal pull-up). If LOW, sets `_force_provision = True`. Also sets `_force_provision = True` when `wifi.ssid` is empty (fresh-device first boot — the normal path into AP mode).
10. `connect_wifi()` — tries each candidate network (primary + `networks` list), up to `retries` per SSID; LED stays OFF until connected, then LED ON. Returns `(wlan, ip)`.
11. **AP mode decision**: if `_force_provision` OR `ip is None`:
    - `start_ap_provisioning(cfg)` — open AP at `192.168.4.1`
    - `io.start_provisioning_blink()` — slow double-blink LED pattern
    - `main()` runs only `start_server(mode="ap")` — no announce, no control loop
12. **STA mode** (normal boot):
    - `state.attach_wlan_info(wlan, cfg)` — caches MAC, IP, hostname, port.
    - Initialises hardware WDT (`WDT(timeout=8000)` — 8-second timeout). Must be created **after** `connect_wifi()` returns (boot WiFi can take 30s+ and would trigger a spurious reboot).
    - **WDT (Watchdog Timer)**: STA mode only (not AP provisioning). Fed from `control_loop`'s 200ms chunked-sleep loop — covers event-loop-wide blocking hangs and control_loop task death. Not fed from an independent task (would miss control_loop death).
    - `main()` async task:
      - `start_metrics()` — launches background event-loop utilisation meter.
      - Spawns `announce_then_retry_once()` — tries POST to hub; if it fails, waits 60 s and retries once.
      - **`announce_then_retry_once` is re-entrant**: safe to call at runtime (not just boot). The caller must cancel any prior announce task before spawning a new one to avoid overlapping heartbeat/LED control.
      - Spawns `control_loop()` — hysteresis loop; also feeds the WDT every 200ms via its chunked-sleep loop. If the loop hangs or dies, the WDT expires → hard reboot → WiFi reconnects + re-announces.
      - Spawns `wifi_watchdog_loop()` — monitors link health; on drop, force-clears the LED, reconnects with backoff (5/10/30/60 s, then caps at 60 s — `_BACKOFF_MS`); on link-up, re-announces (see §Wi-Fi watchdog & runtime re-announce).
      - Spawns `reboot_scheduler_loop()` — syncs NTP after WiFi connect (up to 3 boot attempts spaced 30 s; if still unsynced, retries every 5 min via `_NTP_RETRY_MS`), then polls every 60s; triggers `machine.soft_reset()` at the configured hour/minute (disabled by default). Not spawned in AP mode.
      - `await start_server()` — Microdot HTTP server on `0.0.0.0:<api_port>`.

## Firmware Requirements (mDNS)

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

Runs as a `uasyncio` coroutine every `control.period_s` seconds (default 10 s in `config_loader.py`; matches the checked-in `config.json`).

- **Humidifier**: ON when `humidity <= target − humidity_deadband` (asymmetric low-side deadband). Stays ON until humidity rises above the deadband threshold.
- **Fan**: ON when `humidity > target + hyst_hum` (high-side deadband). Stays ON until humidity drops to `<= target + hyst_hum`.
- **Deadband**: between `target − humidity_deadband` and `target + hyst_hum` neither humidifier nor fan runs — prevents fighting between the two actuators. The deadband is asymmetric: the low-side gap (`humidity_deadband`, default 3) and high-side gap (`hyst_hum`, default 5) are independently configurable.
- **Heater**: ON when `temperature <= target − temperature_deadband` (asymmetric low-side deadband, default 1).
- **Minimum runtime**: once a relay turns ON, it stays ON for at least `min_runtime` seconds (default 30) before it can be turned off. Tracked per-relay via `_on_since` timestamps (ticks_ms). This prevents rapid cycling of relays when readings hover near deadband boundaries.
- Humidifier and fan are **mutually exclusive** — each branch calls the other's `_turn_off()` helper as a safety measure.
- When `control_enabled = False`: keeps sampling the DHT11 every `control.period_s`, calls `relays_off()` once (relays only — LED heartbeat continues), resets `_on_since` tracking, and skips hysteresis actuation until re-enabled. Sampling is decoupled from actuation so `GET /`, `/sensors`, and `/health` keep returning fresh readings.
- API handlers (`POST /outputs`, `POST /control`) also update `_on_since` so min-runtime tracking stays consistent when relays are driven outside the control loop.

**Key design rule**: sensor sampling must remain independent of `control_enabled` (observability vs. actuation separation). `io.all_off()` is for fail-safe/shutdown only; use `io.relays_off()` for control-disabled state.

## AP Provisioning Mode

The Pico enters AP provisioning mode when **any** of these is true:

1. **No Wi-Fi configured** — `wifi.ssid` is empty (factory default; the normal way a fresh unit reaches provisioning). `main.py` sets `_force_provision = True` and `connect_wifi()` skips the STA attempt entirely (returns `(wlan, None)` immediately).
2. **GP0 held LOW at boot** (force-provision jumper, internal pull-up).
3. **Wi-Fi connection failed** after all attempts (`connect_wifi()` retries 3× per candidate SSID).

Details:

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
  "hub_url": "http://<hub_ip>:3000/v1/pico-units/announce",
  "hub_secret": "mushpi-dev-secret",
  "device_name": "pico-unit1",
  "api_port": 5000,
  "control": { "period_s": 10, "hyst_hum": 5, "hyst_temp": 1, "humidity_deadband": 3, "temperature_deadband": 1, "min_runtime": 30 },
  "reboot": { "enabled": false, "hour": 4, "minute": 0, "ntp_host": "pool.ntp.org", "ntp_tz_offset_hours": 0 },
  "pins": { "dht": 4, "humidifier": 6, "fan": 7, "heater": 8 },
  "active_high": false
}
```

- `device_name` must match the `handle` field in `mushpi-server`'s `PicoUnit` entity.
- `hub_url` must include the server's versioned announce path — the `/v1/` prefix (see example above); there is no unversioned announce endpoint.
- `hub_secret` must match the server's `PICO_ANNOUNCE_SECRET` env var. Sent as `X-Pico-Secret` header on every announcement POST.
- `config.json` is gitignored — the local working copy holds the real credentials (untracked, not committed).
- `config_loader.py` provides defaults for all fields **except `hub_secret`** (which has no default — treated as optional `""` by the validator). Example defaults: device_name `"PicoDevice"`, period_s `10`.
- `networks` is optional. If present, `connect_wifi()` tries each in order; first success wins. The primary `ssid`/`password` pair is tried first.
- `pins` is a dict mapping device roles to GPIO pin numbers (`dht`, `humidifier`, `fan`, `heater`). Each value must be an integer in 1–29 (GP0 is reserved for force-provision). Persisted by `POST /setup`; falls back to defaults if missing.
- `active_high` is a boolean controlling relay polarity (`false` = active-low, the safe default for common relay modules). Persisted by `POST /setup`; falls back to `false` if missing.
- `reboot` controls the daily scheduled soft-reboot + NTP sync (`enabled`, `hour`, `minute`, `ntp_host`, `ntp_tz_offset_hours`). Disabled by default (`enabled: false`).
- `control.hyst_temp` is **deprecated / vestigial** — `app/control.py` never reads it (heater control uses `temperature_deadband`). It is still present in the defaults (`config_loader.py`) and validated by `config_validator.py` (int, 0–50), so it is accepted and persisted, but has no effect.

## LED Status Indicators

- **OFF**: WiFi connection failed — check SSID/password. Also the *runtime* link-down state: `wifi_watchdog_loop()` force-clears the LED (`io.led_solid(False)`, which also cancels the heartbeat) while it retries reconnection (`app/wifi.py`).
- **SOLID ON**: WiFi connected but hub announcement failed (or still in progress).
- **BLINKING** (heartbeat): WiFi connected and hub announcement succeeded.
- **PROVISIONING** (slow double-blink): 200ms on / 200ms off / 200ms on / 800ms off — AP mode active, awaiting Wi-Fi credentials via web form.
- **CONFIG ERROR** (3 fast blinks): 150ms on / 150ms off × 3, 1000ms pause, repeating — `config.json` is malformed or has invalid values; check serial console for details. Terminal state — the device does not start any services.

## Coding Rules (Gotchas)

### RAM budget

~520 KB on RP2350 (Pico 2 W); avoid large imports, f-strings, or unnecessary allocations. Call `gc.collect()` at the control-loop iteration boundary (after `evaluate_hysteresis()`, before the chunked-sleep loop) and in the HTTP `after_request` hook (`_cors()`). Per-call overhead is ~1–3ms — acceptable on a 10s+ loop and infrequent HTTP requests. This prevents heap fragmentation from accumulating over weeks of uptime.

### MicroPython quirks

- `urequests` does not support the `json=` kwarg — pass pre-serialised `ujson.dumps(body)` as `data=` with explicit `Content-Type: application/json` header.
- `usocket.setdefaulttimeout()` is used to set per-request timeouts and restored to `None` afterwards.
- DHT11 can return implausible readings; `DHTReader.plausible()` validates `-10 ≤ t ≤ 60` and `0 ≤ h ≤ 100`. **Garbage sensor readings must never leak into `status`**: `DHTReader.read()` writes `status["temperature"]` and `status["humidity"]` ONLY after `plausible()` returns `True`. Implausible readings set `last_sensor_error` but leave the last plausible values in place, so the control loop's `None` guards correctly suppress actuation during sensor faults.
- **Sentinel-based reboot classification**: `machine.reset_cause()` on rp2 is unreliable for distinguishing intentional from watchdog reboots (both `machine.reset()` and `machine.soft_reset()` may report as `WDT_RESET`). Instead, **all intentional reboots** (scheduled, `POST /reboot`) write a `pending_type` sentinel to `uptime.json` via `mark_pending_reboot()` before resetting. `init_uptime()` reads the sentinel first; `reset_cause()` is used only as a fallback for unexpected reboots.
- **No battery-backed RTC**: `time.localtime()` is garbage until NTP-synced. Any wall-clock feature must NTP-sync first (blocking, in the STA boot path — never from an async task). NTP-sync logic lives in `app/reboot_scheduler.py`.

### Wi-Fi watchdog & runtime re-announce (`app/wifi.py`)

- `wifi_watchdog_loop()` polls the link every 5 s. On drop it force-clears the LED (`io.led_solid(False)` — cancels the heartbeat) and retries `reconnect_once_async()` with backoff `_BACKOFF_MS = [5000, 10000, 30000, 60000]` (5/10/30/60 s, capped at 60 s thereafter until it connects).
- On link-**up** it spawns `announce_then_retry_once()` (after cancelling any prior announce task) — the **only non-boot trigger for an announce**. This is how the hub learns the unit's (possibly new) IP after a DHCP change, and the announce re-claims the LED (solid on start, heartbeat on success — see §LED Status Indicators).
- **Module-ownership nuance**: announcing is `app/announce.py`'s concern, but the link-up re-spawn lives in `app/wifi.py` (function-scoped `from .announce import ...` to avoid an import cycle). The split is deliberate: `wifi.py` owns *when* (a WiFi state transition), `announce.py` owns *how*. Don't relocate the re-spawn without updating the LED expectations and the hub's IP-refresh behavior tests.

### Persistence

- **`uptime.json`** is a runtime-persisted file at flash root (like `config.json`). Created by `init_uptime()` on every boot. Schema: `{"cumulative_ms": int, "reboot_done_date": "YYYY-MM-DD" | null, "pending_type": "scheduled" | "soft" | "hard" | null}` — `pending_type` is the reboot-classification sentinel written by `mark_pending_reboot()` and cleared to `null` by `init_uptime()` on the next boot. Device-local only — never sent to the hub. Atomic write via `.tmp` + `os.rename`.
- **`reboot_reason` enum**: the valid values produced by `app/uptime.py` are `scheduled`, `soft`, `hard`, `watchdog`, `power_on`, `unknown`. Document these in the `uptime` response schemas and keep `spec/openapi.yaml` in sync.
- **Persist config** with `save_config(cfg)` — atomic write via `config.json.tmp` + `os.rename`. Never `open('config.json','w')` directly.
- **`POST /provision` `wifi` key** persists to flash. **`POST /setup`** also persists `pins` and `active_high` to flash (survives reboot). Setpoints (via `POST /setpoints`) remain runtime-only (lost on reboot).

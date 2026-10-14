# mushpi-grow — Reference (On-Demand)

Long-tail details and gotchas. **Load only when the task touches these areas** — do not read on every spawn. The always-loaded [`AGENTS.md`](./AGENTS.md) holds the module map, deploy steps, REST API table, shared-state rules, and core coding rules.

Topics covered here: startup sequence · firmware/mDNS requirements · hardware pins · control-loop algorithm · `config.json` schema · AP provisioning · LED states · Wi-Fi watchdog · announce transport & watchdog budgets · MicroPython quirks · RAM/gc · persistence.

---

## Startup Sequence (`main.py`)

1. `IO()` — initialises GPIO, turns onboard LED ON.
2. `DHTReader()` — initialises DHT11 sensor.
3. `load_config()` — reads `config.json` with shallow merge over defaults. Returns `(cfg, load_errors)` — catches malformed JSON and non-object top-level gracefully.
4. `validate_config(cfg)` — validates all fields (device_name, api_port, hub_url, wifi, control, pins, reboot) for type and range correctness, and rejects duplicate GPIO assignments: no two roles may share a pin in the **effective** `pins` map (config.json merged over defaults by `load_config()` — `_append_duplicate_gpio_errors`). Accumulates all errors. If any errors (load or validation): prints them to serial, then enters terminal `error_blink_blocking()` state (3 fast blinks + pause forever). Device does NOT start WiFi, control loop, or server.
5. **Apply persisted GPIO mapping** — reads `pins` and `active_high` from `cfg`, applies them to `state.devices`, then calls `sensor.__init__()` and `io.__init__()` to re-initialise GPIO on the persisted pin assignments and polarity. Runs after validation so bad persisted data can't crash re-init; the prior default-constructed `io`/`sensor` instances are briefly double-inited (harmless — relays are OFF).
6. `init_uptime()` — reads `uptime.json` sentinel first: if `pending_type` is set ("scheduled"/"soft"/"hard"), that's the reboot reason (preserving cumulative for scheduled/soft, resetting for hard). Falls back to `machine.reset_cause()` only when no sentinel exists (unexpected reboots: WDT → "watchdog", PWRON → "power_on"). Writes fresh `uptime.json` to flash.
7. `state.init_system_info(cfg)` — caches board/MicroPython/build metadata.
8. `state.check_mdns_firmware(cfg["device_name"])` — warns if firmware < v1.26.0 (mDNS won't work; see README).
9. **Force-provision check** — reads GP0 (internal pull-up). If LOW, sets `_force_provision = True`. Also sets `_force_provision = True` when `wifi.ssid` is empty (fresh-device first boot — the normal path into AP mode).
10. `connect_wifi()` — tries each candidate network (primary + `networks` list), up to `retries` per SSID; LED stays OFF until connected, then LED ON. Returns `(wlan, ip)`.
11. **AP mode decision**: if `_force_provision` OR `ip is None`:
    - `start_ap_provisioning(cfg)` — open AP at `192.168.4.1`
    - `io.start_provisioning_blink()` — slow double-blink LED pattern
    - `main()` runs only `start_server(mode="ap")` — no announce, no control loop, no watchdog (a provisioning unit is never armed)
12. **STA mode** (normal boot):
    - `state.attach_wlan_info(wlan, cfg)` — caches MAC, IP, hostname, port.
    - **Hub DNS pre-resolution** — at `main.py` **module scope** (STA branch, right after `connect_wifi()` returns and before `asyncio.run(main())`), `announce.presolve_boot_hub(cfg, wlan)` resolves the hub host once and caches it in the module-level `announce._hub` dict. This is the sanctioned blocking boot path — the same slot `connect_wifi()` occupies: nothing is scheduled yet and the `WDT` does not exist yet, so a slow `socket.getaddrinfo()` — which has no timeout of its own — cannot starve or reset anything. It is best-effort: it prints, never raises, skips when `hub_url` is empty, and skips when the link or IP is missing. A dotted-decimal hub host is used directly with no lookup at all. **This is the only name resolution the announce path ever performs** — the NTP sync in `app/reboot_scheduler.py` resolves `reboot.ntp_host` on its own, from a task (see §MicroPython quirks).
    - **WDT (Watchdog Timer)**: STA mode only (not AP provisioning — AP mode never arms one). `main()` constructs `WDT(timeout=8000)` on the STA path **before the announce/control/watchdog tasks are created** (only `start_metrics()`'s idle meter precedes it — see the task list below) and passes it to `control_loop()` as `wdt=`. It must not be created before `connect_wifi()` returns (boot WiFi can take 30s+ and would trigger a spurious reboot). Fed from `control_loop`'s 200 ms chunked-sleep loop — covers event-loop-wide blocking hangs and control_loop task death. Not fed from an independent task (would miss control_loop death). The boot pre-resolution above is what keeps the boot slot's unbounded call (`getaddrinfo()`) out of the armed window; the announce transport bounds everything else, and the NTP sync's own lookup is the one known exposure inside the window (see §Announce transport & watchdog budgets).
    - `main()` async task (`start_metrics()` runs first, before the mode branch — it launches the background event-loop utilisation meter; then these tasks are created **in this order**, and `await start_server()` runs last):
      - Spawns `reboot_scheduler_loop()` — the **NTP sync runs from this task**, not the boot path: up to 3 attempts spaced 30 s at startup, then every 5 min via `_NTP_RETRY_MS` until one lands; then polls every 60 s and triggers `machine.soft_reset()` at the configured hour/minute (disabled by default). Not spawned in AP mode. **Note:** its sync performs a blocking name **resolution** of `reboot.ntp_host` itself — see §Announce transport & watchdog budgets, and the time-semantics bullets in §MicroPython quirks.
      - Spawns `announce_then_retry_once(cfg, wlan, io, stop_event=...)` — tries POST to hub; on failure, retries **forever** on the `_RETRY_BACKOFF_S` schedule (30, 30, 60, 120, 300 s, capped at 300 s thereafter) until an attempt succeeds or `stop_event` fires. `main.py` is its **only** spawner.
      - Spawns `control_loop(cfg, wlan, io, sensor, stop_event=..., wdt=wdt)` — hysteresis loop; the **only** watchdog feed site, every 200 ms inside its chunked-sleep loop. If the loop hangs or dies, the WDT expires → hard reboot → WiFi reconnects + re-announces.
      - Spawns `wifi_watchdog_loop()` — monitors link health; on drop, force-clears the LED, reconnects with backoff (5/10/30/60 s, then caps at 60 s — `_BACKOFF_MS`); on link-up, refreshes cached WLAN info, re-arms the LED heartbeat, and logs — it does **not** re-resolve the hub or spawn an announce (see §Wi-Fi watchdog).
      - **Announce internals**: `announce_then_retry_once()` wraps the coroutine `_post_once_async()` (one POST per attempt, body built by `payload()`) over a hand-rolled `usocket` transport — the old `announce_once_blocking()` helper and the module's `urequests` import are **removed**. Every phase boundary is followed by an `await asyncio.sleep_ms(0)`, so an unreachable hub no longer starves `control_loop()` or the HTTP server. The per-attempt budgets are **3 s connect / 1 s per send·recv slice / 6 s response-header read**, all **firmware constants at module level in `app/announce.py`, not `config.json` keys** — an operator must not be able to dial a blocking announce that starves the control loop. The POST target is resolved by `announce_url()` applied to the configured address, which appends `_ANNOUNCE_PATH` instead of using the value verbatim. Resolution is cached in `announce._hub` (`{"target", "addr"}`, mutated in place) by `_ensure_resolved()`; its only public entry point is `presolve_boot_hub(cfg, wlan)` (boot, best-effort), and the retry path reads the cache but **never resolves**. Only the response status line is parsed — the body is never read, as before — and each attempt prints one line, `announce: attempt N done in <ms> ms (status <code>)`, which is the bench-recordable trace.
      - **`announce_then_retry_once` is re-entrant**: the coroutine itself is safe to call again, but nothing does at runtime any more — the boot task is the only one, and it ends once an attempt is acknowledged (otherwise it backs off forever). If a runtime re-announce is ever reintroduced, its caller must cancel any prior announce task first to avoid overlapping heartbeat/LED control.
      - `await start_server()` — Microdot HTTP server on `0.0.0.0:<api_port>`. Awaited, not spawned, so it is the task that keeps `main()` alive.

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
- Pins can be remapped at runtime via `POST /setup` and are persisted to `config.json` (`pins`, `active_high` keys), surviving reboot. A change that would leave two functions on one GPIO (after merging the partial update with the current mapping) is rejected with `400` before any remap or config write.
- The **boot** duplicate rule (`validate_config()` / `_append_duplicate_gpio_errors`) is separate and stricter than the `POST /setup` merged-view rule: any two roles sharing a GPIO in the effective (default-merged) `pins` map is fatal, with no partial-update allowances — there is no live mapping at boot to merge against. Do not "unify" the two rule sets; the setup rule's allowances (same-pin re-assign, within-request pin swap) must stay. The setup rule is semantics-locked across firmware, `mushpi-mock` and the server's setup proxy; the boot rule exists in the firmware only.

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
  "hub_url": "http://<hub_ip>:3000",
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
- `hub_url` carries the hub's **address only** — scheme, host and port (see example above). The unit appends `/v1/pico-units/announce` itself, via the `_ANNOUNCE_PATH` constant in `app/announce.py`. Surrounding whitespace and trailing slashes are tolerated, and a value that already ends in the announce path is used unchanged, so units flashed with the older full-endpoint form keep working without being re-flashed. An empty value is valid: the unit runs standalone and prints `announce: skipped (hub_url not configured)`.
- `hub_secret` must match the server's `PICO_ANNOUNCE_SECRET` env var. Sent as `X-Pico-Secret` header on every announcement POST.
- There is **no announce timeout key** in `config.json` and adding one is not the fix for a slow hub: the announce budgets are firmware constants (see §Announce transport & watchdog budgets), deliberately not operator-tunable.
- `config.json` is gitignored — the local working copy holds the real credentials (untracked, not committed).
- `config_loader.py` provides defaults for all fields **except `hub_secret`** (which has no default — treated as optional `""` by the validator). Example defaults: device_name `"PicoDevice"`, period_s `10`.
- `networks` is optional. If present, `connect_wifi()` tries each in order; first success wins. The primary `ssid`/`password` pair is tried first.
- `pins` is a dict mapping device roles to GPIO pin numbers (`dht`, `humidifier`, `fan`, `heater`). Each value must be an integer in 1–29 (GP0 is reserved for force-provision) and every role needs a **distinct** GPIO — a collision is a fatal boot error, including one created by a partial `pins` object colliding with a default left in place (e.g. `{"pins":{"humidifier":4}}` duplicates the default `dht: 4`). Correction means uploading a corrected `config.json`. Persisted by `POST /setup`; falls back to defaults if missing.
- `active_high` is a boolean controlling relay polarity (`false` = active-low, the safe default for common relay modules). Persisted by `POST /setup`; falls back to `false` if missing.
- `reboot` controls the daily scheduled soft-reboot + NTP sync (`enabled`, `hour`, `minute`, `ntp_host`, `ntp_tz_offset_hours`). Disabled by default (`enabled: false`). `hour`/`minute` are matched against the unit's **local** clock (`UTC + ntp_tz_offset_hours`), and no API writes these keys — the change path is edit `config.json` + reboot (see the time-semantics bullets in §MicroPython quirks).
- `control.hyst_temp` is **deprecated / vestigial** — `app/control.py` never reads it (heater control uses `temperature_deadband`). It is still present in the defaults (`config_loader.py`) and validated by `config_validator.py` (int, 0–50), so it is accepted and persisted, but has no effect.

## LED Status Indicators

- **OFF**: WiFi connection failed — check SSID/password. Also the *runtime* link-down state: `wifi_watchdog_loop()` force-clears the LED (`io.led_solid(False)`, which also cancels the heartbeat) while it retries reconnection (`app/wifi.py`).
- **SOLID ON**: WiFi connected but hub announcement failed (or still in progress).
- **BLINKING** (heartbeat): WiFi connected and hub announcement succeeded.
- **Note on link recovery**: the link-down force-clear (`led_solid(False)`) stops the heartbeat, and link-up **re-arms it directly** — `wifi_watchdog_loop` calls `io.start_led_heartbeat(period_ms=800, stop_event=...)` after a successful reconnect (the helper cancels any existing heartbeat first, so it cannot double-start; and none is live at that point, since the drop cleared it). A recovered link therefore shows the heartbeat again for an acknowledged unit without needing another announcement. Because wifi.py holds no announce-ack state, the re-arm is unconditional: an *unacknowledged* unit also blinks once its link is back, so after a link cycle BLINKING means "Wi-Fi recovered"; it stops meaning "the hub accepted" until a pending announce attempt settles the LED again (see §Wi-Fi watchdog).
- **PROVISIONING** (slow double-blink): 200ms on / 200ms off / 200ms on / 800ms off — AP mode active, awaiting Wi-Fi credentials via web form.
- **CONFIG ERROR** (3 fast blinks): 150ms on / 150ms off × 3, 1000ms pause, repeating — `config.json` is malformed, has invalid values, or assigns the same GPIO to two device roles; check serial console for details (duplicates name the colliding GPIO and both `pins.<role>` names). Terminal state — the device does not start any services.

## Coding Rules (Gotchas)

### RAM budget

~520 KB on RP2350 (Pico 2 W); avoid large imports, f-strings, or unnecessary allocations. Call `gc.collect()` at the control-loop iteration boundary (after `evaluate_hysteresis()`, before the chunked-sleep loop) and in the HTTP `after_request` hook (`_cors()`). Per-call overhead is ~1–3ms — acceptable on a 10s+ loop and infrequent HTTP requests. This prevents heap fragmentation from accumulating over weeks of uptime.

### MicroPython quirks

- `urequests` does not support the `json=` kwarg — pass pre-serialised `ujson.dumps(body)` as `data=` with explicit `Content-Type: application/json` header. (Legacy guidance: the firmware no longer uses `urequests`, but `app/announce.py` still pre-serialises the body for the same reason — the hand-rolled transport writes `Content-Length` itself.)
- `usocket.setdefaulttimeout()` **does not exist** in this MicroPython build. The announce code used to call it behind a bare `except: pass`, so the failure was silent and the intended per-request timeout never reached a socket. Per-socket `s.settimeout()` is the only timeout control available — do not reintroduce a `setdefaulttimeout` call or a parameter that implies the timeout is configurable.
- **Announce no longer uses `urequests` at all.** `_post_once_async()` builds the request and the socket itself on `usocket`, because the library's behaviour was never dependable from this repo: `lib/urequests.py` is **gitignored**, so every flashed unit carries whatever copy someone uploaded, unversioned and unreconcilable. Its local `timeout=` parameter was **dead code** (`URLOpener` ignores the argument and calls `s.settimeout(5)` itself), and that hardcoded 5 s bounded only two of the three blocking phases — `socket.getaddrinfo()` had **no** timeout and the client's `while 1: s.recv(...)` loop granted **each round a fresh full timeout**, so the worst case was unbounded while `control_loop` held the watchdog's only feed site.
- DHT11 can return implausible readings; `DHTReader.plausible()` validates `-10 ≤ t ≤ 60` and `0 ≤ h ≤ 100`. **Garbage sensor readings must never leak into `status`**: `DHTReader.read()` writes `status["temperature"]` and `status["humidity"]` ONLY after `plausible()` returns `True`. Implausible readings set `last_sensor_error` but leave the last plausible values in place — **so the control loop keeps evaluating against a stale reading while the sensor is faulted**. The `None` guards only suppress actuation *before the first* plausible read. This is **not** fail-safe: from the loop's point of view a faulted sensor is indistinguishable from a working one, so a fault that persists can hold an actuator on a stale reading.
- **Sentinel-based reboot classification**: `machine.reset_cause()` on rp2 is unreliable for distinguishing intentional from watchdog reboots (both `machine.reset()` and `machine.soft_reset()` may report as `WDT_RESET`). Instead, **all intentional reboots** (scheduled, `POST /reboot`) write a `pending_type` sentinel to `uptime.json` via `mark_pending_reboot()` before resetting. `init_uptime()` reads the sentinel first; `reset_cause()` is used only as a fallback for unexpected reboots.
- **No battery-backed RTC — and the clock it does have holds *local* civil time.** `ntptime.settime()` sets the RTC to UTC; `sync_ntp_once()` (`app/reboot_scheduler.py`) then folds `reboot.ntp_tz_offset_hours` into the hour **before** rewriting `machine.RTC()`, so `time.localtime()` returns `UTC + offset` directly, no conversion at read time. Watch the tuple order: `time.localtime()` is `(y, mo, d, hr, mi, sec, wd, yday)` while `machine.RTC().datetime()` is `(y, mo, d, wd, hr, mi, sec, subsec)` — not interchangeable.
  - **`reboot.hour` / `reboot.minute` are compared against that local value** — `reboot_scheduler_loop()` matches `time.localtime()[3]`/`[4]` to the configured hour and minute. With `ntp_tz_offset_hours: 0` (the `config_loader.py` default) the configured hour is therefore a **UTC** hour. Reverse direction: `reboot_utc_hour = (configured_hour − ntp_tz_offset_hours) mod 24`.
  - **Whole hours only.** The offset is an integer validated to **−12…+14** by `config_validator.py`, so half-hour zones cannot be expressed.
  - **No daylight-saving handling anywhere.** The offset is a fixed integer the operator must change by hand when their zone shifts.
  - **Changing the offset requires a reboot**: `reboot_scheduler_loop()` reads it once when the scheduler task starts (`tz_offset = reboot_cfg.get(...)` before the loop), not per iteration — and `cfg` itself is loaded once at boot.
  - **The armed schedule is gated on a landed sync.** Time checks run only when `synced` is true, so with no sync landed no date is observed and no reboot can be armed: an unreachable or unresolvable `ntp_host` means *no scheduled reboot*, not a wrong one. (A failed `time.localtime()` read also clears `synced` and forces a re-sync.)
  - **No boot grace period exists** — `_NTP_RETRY_MS` is retry cadence, not a floor. Once a sync lands, the first 60 s poll matching `hour:minute` on a day not yet stamped reboots, even shortly after boot. The only guards are the sync gate above and the once-per-day stamp (`mark_pending_reboot("scheduled")` writes `reboot_done_date` to `uptime.json`).
  - **Reading the unit's clock on the bench:** the REST API exposes no timestamp, date, or timezone — only durations (`uptime_s`, `total_s`, `since_boot_s`). Use the MicroPico console: `import time; print("%04d-%02d-%02d %02d:%02d:%02d" % time.localtime()[:6])` — the same source the scheduler compares against (`uptime.today_str()` reads `time.localtime()` too).

### Announce transport & watchdog budgets (`app/announce.py`, `main.py`)

- **The rp2 hardware WDT tops out at ~8.388 s and `machine.WDT` cannot be stopped once armed.** So raising the timeout is **never** a mitigation for a long blocking operation: the window cannot go past the hardware ceiling, and an armed watchdog cannot be stood down while a slow call runs. The only available lever is making the blocking operation cooperative — which is what the announce budgets do. Do not "fix" a watchdog reset by lengthening `WDT(timeout=...)`.
- **Why every announce phase is bounded and sliced** (the one-feed-site rule itself is core-tier, stated in `AGENTS.md`): because `control_loop`'s 200 ms chunked-sleep loop is the only thing that keeps the watchdog alive, any *other* task that blocks without yielding starves the feed for as long as it blocks. The announce transport therefore bounds every phase and puts an `await asyncio.sleep_ms(0)` at every phase boundary, so a slow or dead hub costs an announce, never regulation. The arithmetic: the largest single uninterrupted blocking stretch inside an attempt is one `connect()` bounded by `_CONN_TIMEOUT_S` (3 s of the 8 s window); every send/recv slice is bounded by `_IO_SLICE_S` (1 s) with an await between slices, and the header read is capped in *slice count* by `_RESP_BUDGET_MS`. Corollary for anyone adding an async task: your task is the thing that can reset the unit, and there is no second feeder to save it. Never add a feed site elsewhere — the design intent is that a dead `control_loop` stops being fed.
- **`ntptime.settime()` performs the same unbounded name lookup (`getaddrinfo`) inside the armed window** when `reboot.enabled` is true (default `False`) — `app/reboot_scheduler.py` runs it from the scheduler **task**, which is created after the `WDT` exists, so this resolution is *not* covered by the boot-path exception: the boot slot only ever holds the hub lookup. **Known, not fixed here**, and tracked separately: with scheduled reboots disabled the unit does not hit it, and a DNS answer for the NTP pool is normally fast, but the exposure is real and identical in kind to the one the announce transport was fixed for. If you touch the NTP path, this is the thing to revisit.
- **`ussl`-wrapped sockets may not expose `settimeout()`.** `_set_timeout()` therefore applies the timeout to the **wrapper and the raw socket** and treats either landing as success. If the wrapper silently ignores it, the https path's per-slice IO timeout degrades from 1 s to the 3 s connect timeout — still bounded, just coarser. The whole `https` branch is untested on hardware; assume it needs a bench pass before relying on it.
- **Boot-only resolution — the retry path never resolves.** `socket.getaddrinfo()` has no timeout of its own, so the hub name is resolved exactly once: `presolve_boot_hub()` at `main.py` module scope, after `connect_wifi()` returns and **before the `WDT` object is constructed** (the sanctioned blocking boot slot — this extends the existing "main.py boot path" exception in `AGENTS.md`; it must stay outside `main()`, and moving it into a task would put unbounded blocking back inside the loop). Runtime attempts use only the cached `_hub["addr"]`, or an IPv4 literal parsed straight from the config. Consequence, by design: a hub address change, or a boot lookup that failed, is picked up only at the **next boot**; with no usable cached address the attempt logs `no hub address available` and fails without any lookup, and the task keeps backing off. Adding a lookup to the retry path "just in case" is the bug this rule exists to prevent. There is deliberately **no TTL and no background re-resolve** — do not add either as a side effect.
- **The request line stays `HTTP/1.0` and the `Host:` header carries no port**, purely for wire compatibility with what the old `urequests` client sent the hub. Modernising either one (`HTTP/1.1`, `Host: name:port`) is a hub-side contract change: check the server's body-parser and the mock before touching it. `Content-Length` is written **exactly once** by the firmware — the old `urequests` header dict deliberately omitted it because the client appended its own copy, and a duplicate is what the old code comment warned would make the hub's body-parser reject the payload.
- **The retry schedule is forever, with backoff.** After a failed attempt, `announce_then_retry_once()` waits per `_RETRY_BACKOFF_S = (30, 30, 60, 120, 300)` seconds and retries — after the tuple is exhausted it repeats the last entry (300 s cap) until an attempt succeeds or the unit shuts down. `main.py` is the **only** spawner: the Wi-Fi watchdog no longer re-announces on link-up (see §Wi-Fi watchdog). The waits are pure `await asyncio.sleep()` — they hold nothing, so no interval, however long, can starve the watchdog; what keeps the armed window safe is the per-phase bounding above plus boot-only resolution.

### Wi-Fi watchdog (`app/wifi.py`)

- `wifi_watchdog_loop()` polls the link every 5 s. On drop it force-clears the LED (`io.led_solid(False)` — cancels the heartbeat) and retries `reconnect_once_async()` with backoff `_BACKOFF_MS = [5000, 10000, 30000, 60000]` (5/10/30/60 s, capped at 60 s thereafter until it connects).
- On link-**up** it refreshes the cached WLAN info (`attach_wlan_info(wlan, cfg)`), reads the new IP, re-arms the LED heartbeat (`io.start_led_heartbeat(period_ms=800, stop_event=stop_event)` — the helper is cancel-first, so it cannot double-start, and it is a cold start here because the link-down force-clear already stopped any heartbeat), and logs. There is **no hub-cache invalidation** (no runtime lookup exists to be invalidated — see §Announce transport & watchdog budgets) and **no announce respawn**. What covers the respawn's old jobs: if the boot announce is still unacknowledged, its task is still running its backoff loop and the first attempt after link-up carries the new IP (the payload reads the same `state` cache `attach_wlan_info()` refreshed); if the boot announce already succeeded, that task has *returned* and nothing re-announces this boot — the hub's 60 s poll refreshes the unit's stored IP over `GET /` independently of any announce. The heartbeat re-arm replaced the respawn's other former job (re-lighting the LED after a link bounce); it is unconditional because wifi.py holds no announce-ack state — see the §LED Status Indicators note. Do not reintroduce a respawn for LED purposes or otherwise (it created two forever-retrying announce tasks).
- **Module-ownership**: announcing is entirely `app/announce.py`'s concern, and `main.py` is its only spawner — `wifi.py` no longer imports `announce` at all. The removed link-up respawn is deliberate, not an oversight: with the boot task retrying forever, respawning created two forever-retrying announce tasks (doubled attempt logs, a possible double heartbeat) and bought nothing. Do not reintroduce a runtime re-announce without first solving that overlap — and note the link-up moment was also the only place a runtime lookup could once happen; boot-only resolution closed that too.

### Persistence

- **`uptime.json`** is a runtime-persisted file at flash root (like `config.json`). Created by `init_uptime()` on every boot. Schema: `{"cumulative_ms": int, "reboot_done_date": "YYYY-MM-DD" | null, "pending_type": "scheduled" | "soft" | "hard" | null}` — `pending_type` is the reboot-classification sentinel written by `mark_pending_reboot()` and cleared to `null` by `init_uptime()` on the next boot. Device-local only — never sent to the hub. Atomic write via `.tmp` + `os.rename`.
- **`reboot_reason` enum**: the valid values produced by `app/uptime.py` are `scheduled`, `soft`, `hard`, `watchdog`, `power_on`, `unknown`. Document these in the `uptime` response schemas and keep `spec/openapi.yaml` in sync.
- **Persist config** with `save_config(cfg)` — atomic write via `config.json.tmp` + `os.rename`. Never `open('config.json','w')` directly.
- **`POST /provision` `wifi` key** persists to flash. **`POST /setup`** also persists `pins` and `active_high` to flash (survives reboot). Setpoints (via `POST /setpoints`) remain runtime-only (lost on reboot).

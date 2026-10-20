import gc
import uasyncio as asyncio
import utime as time

from .state import status, setpoints

# Safety threshold for offline (AP) operation: when the unit runs without a
# network, a sensor fault that leaves the newest plausible reading older than
# this many seconds cuts the outputs instead of regulating on stale values
# (see control_loop's sensor_fault_cut). A firmware constant, deliberately NOT
# a config.json key — same philosophy as the announce budgets: an
# operator-tunable safety threshold invites misconfiguration.
_SENSOR_STALE_CUT_S = 120

# Offline sensor-fault latch: True between the cut and the next fresh
# plausible reading. Written ONLY by control_loop() when it runs with
# sensor_fault_cut=True (offline mode), so a connected unit never latches it.
# Read by api.py's immediate POST /setpoints re-evaluation — see fault_latched().
_sensor_fault_hold = False

# Per-relay "time switched ON" in ticks_ms; None = not currently ON-or-in-runtime-window
_on_since = {"humidifier": None, "fan": None, "heater": None}

control_enabled_event = asyncio.Event()
control_enabled_event.set()

# Boot-time seed for control_loop()'s `relays_latched` local. The loop turns the
# relays off once when it finds control disabled; a persisted hand-set position
# restored at boot must not be wiped by that first pass, so runtime_state.restore()
# primes this to True when it re-drives the relays. Written ONLY by
# prime_control_enabled() below; False keeps a cold boot behaving as before.
_relays_latched_init = False

def set_control_enabled(enabled: bool):
    if enabled:
        control_enabled_event.set()
    else:
        control_enabled_event.clear()

def prime_control_enabled(enabled, relays_latched=False):
    """Boot-only restore of the persisted loop flag (sole caller:
    app/runtime_state.restore()). Sets/clears control_enabled_event and is the
    ONLY writer of _relays_latched_init.

    relays_latched=True declares that the disabled-state relay positions are
    already live on the outputs, so control_loop() must skip its one-shot
    relays_off(). Unlike set_control_enabled(), this never acts on relays — the
    restore path in runtime_state owns the physical writes."""
    global _relays_latched_init
    _relays_latched_init = bool(relays_latched)
    set_control_enabled(enabled)

def is_control_enabled():
    return control_enabled_event.is_set()

def fault_latched():
    """True while the offline sensor-fault cut is holding the outputs off
    (stale reading, sensor_fault_cut mode). The only writer is control_loop()
    with sensor_fault_cut=True, so this is never set on a connected unit.
    api.py consults it so POST /setpoints' immediate re-evaluation cannot
    re-energise a relay against stale values while the cut is active."""
    return _sensor_fault_hold

def _can_turn_off(relay, min_runtime_ms):
    since = _on_since[relay]
    if since is None:
        return True
    elapsed_ms = time.ticks_diff(time.ticks_ms(), since)
    return elapsed_ms >= min_runtime_ms

def _turn_on(io, relay):
    if relay == "humidifier": io.hum_on()
    elif relay == "fan": io.fan_on()
    elif relay == "heater": io.heat_on()
    _on_since[relay] = time.ticks_ms()

def _turn_off(io, relay, min_runtime_ms):
    if not _can_turn_off(relay, min_runtime_ms):
        return
    if relay == "humidifier": io.hum_off()
    elif relay == "fan": io.fan_off()
    elif relay == "heater": io.heat_off()
    _on_since[relay] = None

def reset_on_since():
    for relay in _on_since:
        _on_since[relay] = None

def mark_relay_off(relay):
    _on_since[relay] = None

def mark_relay_on(relay):
    _on_since[relay] = time.ticks_ms()

def evaluate_hysteresis(cfg, io):
    """Actuation step with asymmetric deadband and minimum runtime gate.
    Humidifier ON at h <= target - humidity_deadband (low-side).
    Fan ON at h > target + hyst_hum (high-side).
    Heater ON at t <= target - temperature_deadband.
    Relays stay ON for at least min_runtime seconds before they can turn off.
    Does NOT check control_enabled; the caller is responsible for gating.
    Reads status/setpoints live (no snapshots)."""
    Hh = cfg["control"]["hyst_hum"]
    Hd = cfg["control"]["humidity_deadband"]
    Td = cfg["control"]["temperature_deadband"]
    Mr = cfg["control"]["min_runtime"] * 1000

    t = status["temperature"]
    h = status["humidity"]
    ht = setpoints["humidity"]
    tt = setpoints["temperature"]

    # Humidity control — humidifier and fan are mutually exclusive
    if h is not None:
        if h <= ht - Hd:
            # Below low-side deadband: run humidifier to push UP
            _turn_on(io, "humidifier")
            _turn_off(io, "fan", Mr)
        elif h > ht + Hh:
            # Above high-side deadband: run fan to push DOWN
            _turn_on(io, "fan")
            _turn_off(io, "humidifier", Mr)
        else:
            # Inside deadband — neither runs (gated by min_runtime)
            _turn_off(io, "humidifier", Mr)
            _turn_off(io, "fan", Mr)

    # Temperature — heater (adds heat), no cooler counterpart
    if t is not None:
        if t <= tt - Td:
            _turn_on(io, "heater")
        else:
            _turn_off(io, "heater", Mr)

async def control_loop(cfg, wlan, io, sensor, stop_event=None, wdt=None,
                       sensor_fault_cut=False):
    """Hysteresis control loop. sensor_fault_cut=True (wired by main.py for
    offline/AP operation) adds the stale-reading safety cut described on
    _SENSOR_STALE_CUT_S. The parameter keeps WiFi knowledge out of this module
    — the loop itself never inspects the link; mode-gated behaviour is passed
    in by the caller."""
    P = cfg["control"]["period_s"]
    # Primed from runtime.json at boot (see prime_control_enabled): a restored
    # hand-set position counts as already latched-off-safe, so the first pass of
    # a disabled loop does not relays_off() and wipe it.
    relays_latched = _relays_latched_init
    global _sensor_fault_hold

    while not (stop_event and stop_event.is_set()):
        # WiFi monitoring/reconnect handled by wifi_watchdog_loop

        # Always sample the sensor so API/server polling stays fresh
        # regardless of whether control is enabled (observability vs actuation).
        try:
            sensor.read()
        except:
            pass

        if not control_enabled_event.is_set():
            # Control disabled: turn off relays once, keep LED heartbeat alive.
            if not relays_latched:
                try:
                    io.relays_off()
                    reset_on_since()
                    relays_latched = True
                except:
                    pass
        else:
            # Control enabled: reset latch and run hysteresis actuation.
            relays_latched = False
            if sensor_fault_cut:
                # Offline mode has no hub watching the readings, so a faulted
                # sensor must not keep driving relays from stale values.
                # time.time() deltas are valid offline — the RTC ticks without
                # NTP, and sensor.py computes last_ok_age_s the same way.
                # last_sensor_ok_at None (never read) needs no special case:
                # evaluate_hysteresis's existing None guards already suppress
                # actuation before the first plausible read.
                last_ok = status["last_sensor_ok_at"]
                stale = last_ok is not None and (time.time() - last_ok) > _SENSOR_STALE_CUT_S
                if stale:
                    if not _sensor_fault_hold:
                        _sensor_fault_hold = True
                        print("control: sensor stale beyond " + str(_SENSOR_STALE_CUT_S) +
                              "s - outputs cut until a good reading returns")
                    # Hold the cut every iteration (idempotent GPIO writes): a
                    # manual POST /outputs must not keep a relay energised
                    # against a stale reading while the cut is active.
                    io.relays_off()
                    reset_on_since()
                else:
                    if _sensor_fault_hold:
                        _sensor_fault_hold = False
                        print("control: fresh sensor read - resuming regulation")
                    evaluate_hysteresis(cfg, io)
            else:
                evaluate_hysteresis(cfg, io)

        # Reclaim fragmented heap before sleeping (prevents OOM on long runs)
        try:
            gc.collect()
        except:
            pass

        # Sleep in small chunks for responsive shutdown
        chunks = (P * 1000) // 200
        for _ in range(chunks):
            if stop_event and stop_event.is_set():
                return
            if wdt:
                wdt.feed()
            await asyncio.sleep_ms(200)


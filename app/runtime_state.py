# runtime_state.py — device-local persistence of grower-chosen control state.
#
# Holds the three things a grower sets by hand that must survive a unit restart:
# the targets (setpoints), the control-loop on/off flag, and — when the loop is
# off — the hand-set fan/humidifier positions. Stored in runtime.json at flash
# root with the same atomic `.tmp` + `os.rename` pattern proven by uptime.py.
#
# Deliberately NOT config.json. config.json is the operator-flashed provisioning
# artifact (gitignored, uploaded by MicroPico, rewritten wholesale by
# save_config()). Persisting grower state there would wipe the grower's targets
# on the next re-deploy — the exact failure this module exists to fix — and
# rewrite Wi-Fi credentials and hub_secret on every target tweak. Do not
# "unify" the two files (see REFERENCE §Persistence).
#
# The heater is never persisted and never restored to ON: the schema has no
# heater key, save_outputs() takes no heater parameter, and restore() drives
# heat_off() unconditionally before applying anything.
#
# Durability is for grower intent only. The control loop never calls this
# module — loop-driven relay switching must not wear the flash (control.py,
# hw.py and sensor.py must never import runtime_state). Only the three POST
# handlers in api.py write here.

import ujson

# os/uos compatibility
try:
    import os
except ImportError:
    import uos as os

from .state import setpoints
from . import control

_PATH = "runtime.json"
_VERSION = 1

# Restore-time clamp bounds — the unit page's slider ranges (app/unit.html:
# target temperature 0–50 °C, target humidity 10–90 %). Defensive clamp at
# RESTORE time only; the live POST /setpoints endpoint does no range check.
_TEMP_MIN = 0
_TEMP_MAX = 50
_HUM_MIN = 10
_HUM_MAX = 90


def _atomic_write(data):
    """Atomically write runtime.json via .tmp + os.rename.

    Unlike uptime.py's equivalent, a failure is NOT swallowed: it propagates to
    the caller, which logs it. POST handlers catch so a flash write fault never
    fails the request, and restore() is read-only. Callers own the logging."""
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        ujson.dump(data, f)
        f.flush()
    os.rename(tmp, _PATH)


def _clamped_int(val, lo, hi):
    """Return val clamped into [lo, hi] as an int, or None if not a usable int.

    bool is a subclass of int, so a True/False setpoint is corruption, not a
    value — reject it explicitly. A float is likewise rejected: the contract for
    setpoints is integer degrees/percent."""
    if isinstance(val, bool) or not isinstance(val, int):
        return None
    if val < lo:
        return lo
    if val > hi:
        return hi
    return val


def _as_bool(val):
    """Return val if it is a strict bool, else None."""
    return val if isinstance(val, bool) else None


def _load():
    """Read runtime.json and return a normalised dict. Never raises.

    A missing, damaged, or unreadable file (first boot, corrupted flash) yields
    an empty dict. Individual bad fields are dropped so the good ones survive —
    restore() then falls back to the state.py defaults (25 / 60, loop on, all
    outputs off) for anything absent. A top-level version mismatch yields an
    empty dict, so an unknown future schema is ignored rather than misread.
    Keys this file does not know (e.g. a hand-added outputs.heater) are dropped
    by the same normalisation and never re-persisted."""
    try:
        with open(_PATH, "r") as f:
            raw = ujson.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return {}

    out = {}

    sp = raw.get("setpoints")
    if isinstance(sp, dict):
        good = {}
        t = _clamped_int(sp.get("temperature"), _TEMP_MIN, _TEMP_MAX)
        if t is not None:
            good["temperature"] = t
        h = _clamped_int(sp.get("humidity"), _HUM_MIN, _HUM_MAX)
        if h is not None:
            good["humidity"] = h
        if good:
            out["setpoints"] = good

    ce = _as_bool(raw.get("control_enabled"))
    if ce is not None:
        out["control_enabled"] = ce

    outs = raw.get("outputs")
    if isinstance(outs, dict):
        good = {}
        fan = _as_bool(outs.get("fan"))
        if fan is not None:
            good["fan"] = fan
        hum = _as_bool(outs.get("humidifier"))
        if hum is not None:
            good["humidifier"] = hum
        if good:
            out["outputs"] = good

    return out


def _write_field(key, value):
    """load → replace one top-level key → atomic write (the shared body of the
    three save_* writers below). Raises on flash failure; callers log."""
    data = _load()
    data["version"] = _VERSION
    data[key] = value
    _atomic_write(data)


def save_setpoints(temperature, humidity):
    """Persist grower-chosen targets (POST /setpoints). Raises on flash failure."""
    _write_field("setpoints",
                 {"temperature": int(temperature), "humidity": int(humidity)})


def save_control_enabled(enabled):
    """Persist the control-loop on/off flag (POST /control). Raises on flash failure."""
    _write_field("control_enabled", bool(enabled))


def save_outputs(fan, humidifier):
    """Persist hand-set fan/humidifier positions (POST /outputs). Raises on flash failure.

    There is no heater parameter by design: the heater is never restored to ON
    across a restart."""
    _write_field("outputs", {"fan": bool(fan), "humidifier": bool(humidifier)})


def restore(cfg, io, ap_mode):
    """Single boot rehydrate entry point — called once from main.py at module
    scope, before asyncio.run() starts the event loop. Never raises into the
    boot path beyond what _load() cannot already see; flash is read-only here.

    Order matters (see REFERENCE §Startup Sequence):
      1. targets are written IN PLACE into state.setpoints — the dict is shared
         with api.py/control.py and must never be reassigned;
      2. the loop flag and control_loop()'s first-pass relay latch are primed
         together via control.prime_control_enabled() — without a primed latch,
         a disabled loop's first pass would call relays_off() and wipe the
         restored hand-set positions within one period_s;
      3. loop off (STA only): heater driven off unconditionally FIRST, then
         fan/humidifier applied per the stored values;
      4. loop on: no relay is touched — the loop's own hysteresis owns actuation;
      5. AP mode: view-only provisioning — targets and loop flag only, all relay
         writes skipped so the outputs stay boot-safe OFF.

    cfg is accepted for signature parity with the other boot callers
    (presolve_boot_hub, connect_wifi); defaults come from state.py, not config,
    so it is not read here."""
    data = _load()

    # 1. Targets — mutate the shared dict in place, key by key.
    sp = data.get("setpoints")
    if isinstance(sp, dict):
        for key in ("temperature", "humidity"):
            if key in sp:
                setpoints[key] = sp[key]
                print("runtime: restored setpoints." + key + " = " + str(sp[key]))

    # 2/3/4/5. Loop flag (+ relay restore when it is off).
    enabled = data.get("control_enabled", True)
    if enabled:
        control.prime_control_enabled(True)
        print("runtime: control loop restored ENABLED")
        # Loop on: touch no relays — hysteresis drives them from the targets.
        return

    if ap_mode:
        # AP provisioning is view-only: the control loop never runs and relays
        # stay boot-safe OFF. Still apply the flag so the page reflects the
        # grower's persisted choice after the unit joins Wi-Fi.
        control.prime_control_enabled(False)
        print("runtime: control loop restored DISABLED")
        print("runtime: AP mode — outputs not restored (relays stay off)")
        return

    # Loop off in STA mode: prime the latch so the restored positions below
    # survive control_loop()'s first pass, then drive the relays.
    control.prime_control_enabled(False, relays_latched=True)
    print("runtime: control loop restored DISABLED")

    # Heater first, unconditionally — it is never restored to ON.
    io.heat_off()
    print("runtime: heater held OFF (never persisted)")

    outs = data.get("outputs")
    if isinstance(outs, dict):
        fan = outs.get("fan")
        if fan is not None:
            if fan:
                io.fan_on()
            else:
                io.fan_off()
            print("runtime: restored outputs.fan = " + str(fan))
        hum = outs.get("humidifier")
        if hum is not None:
            if hum:
                io.hum_on()
            else:
                io.hum_off()
            print("runtime: restored outputs.humidifier = " + str(hum))
    else:
        print("runtime: no persisted outputs (relays stay off)")

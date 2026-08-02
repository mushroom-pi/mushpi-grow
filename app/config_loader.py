import ujson
try:
    import os
except ImportError:
    import uos as os

# Units ship with empty SSID — first boot enters AP provisioning mode
_DEFAULT = {
    "wifi": {"ssid": "", "password": "", "networks": []},
    "hub_url": "",
    "device_name": "PicoDevice",
    "api_port": 5000,
    "control": {"period_s": 5, "hyst_hum": 5, "hyst_temp": 1},
    "reboot": {"enabled": False, "hour": 4, "minute": 0,
               "ntp_host": "pool.ntp.org", "ntp_tz_offset_hours": 0},
    "pins": {"dht": 4, "humidifier": 6, "fan": 7, "heater": 8},
    "active_high": False,
}

def _default_copy():
    cfg = _DEFAULT.copy()
    cfg["wifi"] = _DEFAULT["wifi"].copy()
    cfg["control"] = _DEFAULT["control"].copy()
    cfg["reboot"] = _DEFAULT["reboot"].copy()
    cfg["pins"] = _DEFAULT["pins"].copy()
    return cfg

def load_config(path="config.json"):
    cfg = _default_copy()
    load_errors = []
    try:
        with open(path) as f:
            data = ujson.load(f)
            if not isinstance(data, dict):
                return _default_copy(), ["ERROR: config.json top-level value must be a dict"]
            # shallow merge
            for k, v in data.items():
                if isinstance(v, dict) and k in cfg:
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except OSError:
        print("config: using defaults (no config.json)")
    except ValueError as e:
        return _default_copy(), ["ERROR: config.json contains malformed JSON: " + str(e)]
    except (TypeError, AttributeError) as e:
        return _default_copy(), ["ERROR: config.json parse failed: " + str(e)]
    return cfg, load_errors

def save_config(cfg, path="config.json"):
    """Atomic write: serialize to .tmp then rename over path."""
    tmp = path + ".tmp"
    # delete stale tmp if it exists
    try:
        os.remove(tmp)
    except OSError:
        pass
    try:
        with open(tmp, "w") as f:
            f.write(ujson.dumps(cfg))
        os.rename(tmp, path)
        print("config: saved to", path)
    except Exception as e:
        print("config: save failed:", e)
        raise

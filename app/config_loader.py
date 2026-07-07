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
}

def load_config(path="config.json"):
    cfg = _DEFAULT.copy()
    # deep-copy the nested wifi dict so defaults aren't mutated
    cfg["wifi"] = _DEFAULT["wifi"].copy()
    cfg["control"] = _DEFAULT["control"].copy()
    try:
        with open(path) as f:
            data = ujson.load(f)
            # shallow merge
            for k, v in data.items():
                if isinstance(v, dict) and k in cfg:
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except OSError:
        print("config: using defaults (no config.json)")
    return cfg

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

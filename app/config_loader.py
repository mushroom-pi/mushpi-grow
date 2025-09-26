import ujson

_DEFAULT = {
    "wifi": {"ssid": "", "password": ""},
    "hub_url": "",
    "device_name": "PicoDevice",
    "api_port": 5000,
    "control": {"period_s": 5, "hyst_hum": 5, "hyst_temp": 1},
}

def load_config(path="config.json"):
    cfg = _DEFAULT.copy()
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

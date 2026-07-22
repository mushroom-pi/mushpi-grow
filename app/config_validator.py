def _require_int(path, val, errors):
    if isinstance(val, bool) or not isinstance(val, int):
        errors.append("ERROR: " + path + " must be an integer, got " + repr(val))
        return False
    return True


def _require_str(path, val, errors):
    if not isinstance(val, str):
        errors.append("ERROR: " + path + " must be a string")
        return False
    return True


def _range_check(path, val, lo, hi, errors):
    if val < lo or val > hi:
        errors.append("ERROR: " + path + " must be between " + str(lo) + " and " + str(hi) + ", got " + str(val))


def validate_config(cfg):
    errors = []

    # device_name: str, non-empty, reject bool
    dn = cfg.get("device_name")
    if isinstance(dn, bool) or not isinstance(dn, str):
        errors.append("ERROR: device_name must be a string")
    elif dn == "":
        errors.append("ERROR: device_name must be a non-empty string")

    # api_port: int (reject bool), 1..65535
    ap = cfg.get("api_port")
    if _require_int("api_port", ap, errors):
        _range_check("api_port", ap, 1, 65535, errors)

    # hub_url: str, empty OK, if non-empty must start with "http"
    hu = cfg.get("hub_url")
    if not isinstance(hu, str):
        errors.append("ERROR: hub_url must be a string")
    elif hu != "" and not hu.startswith("http"):
        errors.append("ERROR: hub_url must start with 'http'")

    # hub_secret: optional, but if present must be str
    hs = cfg.get("hub_secret", "")
    if not isinstance(hs, str):
        errors.append("ERROR: hub_secret must be a string")

    # wifi: must be dict
    wifi = cfg.get("wifi")
    if not isinstance(wifi, dict):
        errors.append("ERROR: wifi must be a dict")
    else:
        # wifi.ssid: str, empty OK
        ws = wifi.get("ssid")
        _require_str("wifi.ssid", ws, errors)

        # wifi.password: str
        wp = wifi.get("password")
        _require_str("wifi.password", wp, errors)

        # wifi.networks: optional, if present must be list
        if "networks" in wifi:
            nets = wifi["networks"]
            if not isinstance(nets, list):
                errors.append("ERROR: wifi.networks must be a list")
            else:
                for i, entry in enumerate(nets):
                    pfx = "wifi.networks[" + str(i) + "]"
                    if not isinstance(entry, dict):
                        errors.append("ERROR: " + pfx + " must be a dict")
                        continue
                    ns = entry.get("ssid")
                    if not isinstance(ns, str):
                        errors.append("ERROR: " + pfx + ".ssid must be a string")
                    elif ns == "":
                        errors.append("ERROR: " + pfx + ".ssid must be a non-empty string")
                    np = entry.get("password")
                    _require_str(pfx + ".password", np, errors)

    # control: must be dict
    ctrl = cfg.get("control")
    if not isinstance(ctrl, dict):
        errors.append("ERROR: control must be a dict")
    else:
        ps = ctrl.get("period_s")
        if _require_int("control.period_s", ps, errors):
            _range_check("control.period_s", ps, 1, 3600, errors)

        hh = ctrl.get("hyst_hum")
        if _require_int("control.hyst_hum", hh, errors):
            _range_check("control.hyst_hum", hh, 0, 100, errors)

        ht = ctrl.get("hyst_temp")
        if _require_int("control.hyst_temp", ht, errors):
            _range_check("control.hyst_temp", ht, 0, 50, errors)

    return errors

import uasyncio as asyncio

from microdot import Microdot, Response

from .state import status, setpoints, devices, get_system_info
from .metrics import system_snapshot
from .control import is_control_enabled, set_control_enabled, evaluate_hysteresis
from .config_loader import save_config
from .shutdown import reboot

try:
    with open('app/provision.html', 'r') as f:
        _PROVISIONING_HTML = f.read()
except OSError:
    _PROVISIONING_HTML = '<html><body><h1>MushPi</h1><p>Error loading provisioning form.</p></body></html>'

def make_app(cfg, wlan, io, sensor, mode="sta"):
    app = Microdot()
    Response.default_content_type = 'application/json'

    # CORS headers on every response
    @app.after_request
    def _cors(req, res):
        res.headers['Access-Control-Allow-Origin'] = '*'
        res.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
        res.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        res.headers['Access-Control-Max-Age'] = '600'
        return res

    # Override OPTIONS handler to include CORS headers
    # (after_request is skipped for OPTIONS in Microdot)
    def _options_with_cors(req):
        allow = []
        for route_methods, route_pattern, _, _, _ in app.url_map:
            if route_pattern.match(req.path) is not None:
                allow.extend(route_methods)
        if 'GET' in allow:
            allow.append('HEAD')
        allow.append('OPTIONS')
        return {
            'Allow': ', '.join(allow),
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '600',
        }
    app.options_handler = _options_with_cors

    @app.get('/ping')
    def _pin(req):
        return

    @app.get('/health')
    def _health(req):
        return system_snapshot(wlan)

    @app.get('/system')
    def system_info(req):
        return get_system_info()

    @app.get('/setpoints')
    def _getsp(req): return setpoints

    @app.post('/setpoints')
    def _setsp(req):
        try:
            data = req.json
            if "temperature" in data: setpoints["temperature"] = int(data["temperature"])
            if "humidity" in data:    setpoints["humidity"]    = int(data["humidity"])
            print("Updated setpoints to", setpoints)

            # Immediate re-evaluation so relays react now rather than waiting
            # for the next control_loop tick. Only when control is enabled —
            # if disabled, the loop owns relay state (latched off).
            if is_control_enabled():
                try:
                    evaluate_hysteresis(cfg, io)
                except Exception as e:
                    print("Immediate hysteresis eval failed:", e)

            return setpoints
        except Exception as e:
            return { "error": str(e)}, 400

    @app.get('/setup')
    def _getdevices(req): return devices

    @app.post('/setup')
    def _setdevices(req):
        try:
            data = req.json
            if "pins" in data:
                pins = data["pins"]
                if "dht" in pins: sensor.remap(pins)
                if "humidifier" in pins: io.remap('humidifier', pins)
                if "fan" in pins: io.remap('fan', pins)
                if "heater" in pins: io.remap('heater', pins)
            if "active_high" in data: devices["active_high"] = bool(data["active_high"])
            print("Updated devices to", devices)
            return devices
        except Exception as e:
            return { "error": str(e) }, 400

    @app.post('/provision')
    def _provision(req):
        try:
            data = req.json
            wifi_data = data.get("wifi") if isinstance(data, dict) else None
            if not isinstance(wifi_data, dict):
                return {"error": "wifi object is required"}, 400
            ssid = wifi_data.get("ssid", "")
            if not ssid or not isinstance(ssid, str):
                return {"error": "wifi.ssid is required"}, 400
            cfg["wifi"]["ssid"] = ssid
            if "password" in wifi_data:
                cfg["wifi"]["password"] = wifi_data["password"]
            if "networks" in wifi_data:
                cfg["wifi"]["networks"] = wifi_data["networks"]
            try:
                save_config(cfg)
            except Exception as e:
                return {"error": str(e)}, 400
            print("WiFi credentials saved, rebooting in 2s...")
            asyncio.create_task(reboot(wlan=None, io=io, sensor=sensor, delay_ms=2000))
            return {"ok": True, "wifi": {"ssid": cfg["wifi"]["ssid"]}}
        except Exception as e:
            return {"error": str(e)}, 400

    @app.get('/sensors')
    def _status(req):
        """
        Returns readings + current device on/off state.
        NOTE: leaves the control loop as the source of truth for reads.
              If you want an on-demand fresh read, call with ?force=1
        """
        try:
            # Optional fresh read: /status?force=1
            if req.args.get('force'):
                sensor.d.measure()

            return sensor.get_latest_dht_read()
        except Exception as e:
            status["last_sensor_error"] = str(e)
            return {"ok": False, "error": status["last_sensor_error"]}, 500

    @app.get('/outputs')
    def _getoutputs(req):
      return {
          "fan": status["fan"],
          "humidifier": status["humidifier"],
          "heater": status["heater"],
      }

    @app.post('/outputs')
    def _setoutputs(req):
        try:
            data = req.json or {}
            if "humidifier" not in data:
                return ({ "ok": False, "error": 'the body needs to include "humidifier" status (true/false)' }, 422)
            if "fan" not in data:
                return ({ "ok": False, "error": 'the body needs to include "fan" status (true/false)' }, 422)
            if "heater" not in data:
                return ({ "ok": False, "error": 'the body needs to include "heater" status (true/false)' }, 422)

            fan = bool(data["fan"])
            humidifier = bool(data["humidifier"])
            heater = bool(data["heater"])
            io.write(io.hum, humidifier); status["humidifier"] = humidifier
            io.write(io.fan, fan); status["fan"] = fan
            io.write(io.heat, heater); status["heater"] = heater

            return { "ok": True, "fan": fan, "humidifier": humidifier, "heater": heater }
        except Exception as e:
            return { "ok": False, "error": str(e) }, 400

    @app.get('/control')
    def _get_control(req):
        return {"enabled": is_control_enabled()}

    @app.post('/control')
    def _set_control(req):
        try:
            body = req.json or {}
        except Exception:
            body = {}
        if "enabled" not in body or not isinstance(body["enabled"], bool):
            return ({"error": 'send {"enabled": true|false}'}, 422)

        enabled = body["enabled"]
        set_control_enabled(enabled)

        # Optional: immediately force relays safe when disabling (LED heartbeat stays alive)
        if not enabled and hasattr(io, "relays_off"):
            try: io.relays_off()
            except: pass

        return {"enabled": enabled}

    @app.get("/")
    def _get_all(req):
        if mode == "ap":
            html = _PROVISIONING_HTML.replace("DEVNAME", cfg.get("device_name", "pico"))
            return Response(html, status_code=200, headers={'Content-Type': 'text/html'})
        return {
            "system": get_system_info(),
            "health": system_snapshot(wlan),
            "devices": devices,
            "sensors": {
                "dht": sensor.get_latest_dht_read(),
            },
            "outputs": {
                "fan": status["fan"],
                "humidifier": status["humidifier"],
                "heater": status["heater"],
            },
            "setpoints": setpoints,
            "control_loop_enabled": is_control_enabled()
        }

    return app

async def start_server(cfg, wlan, io, sensor, mode="sta"):
    app = make_app(cfg, wlan, io, sensor, mode)
    await app.start_server(host='0.0.0.0', port=cfg["api_port"])

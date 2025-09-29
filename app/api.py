import time
from microdot import Microdot, Response

from .state import status, setpoints, devices
from .metrics import system_snapshot

def make_app(cfg, wlan, io, sensor):
    app = Microdot()
    Response.default_content_type = 'application/json'

    @app.get('/ping')
    def _pin(req):
        return

    @app.get('/health')
    def _health(req):
        return {
            "device": cfg["device_name"],
            "system": system_snapshot(wlan),
        }

    @app.get('/setpoints')
    def _getsp(req): return setpoints

    @app.post('/setpoints')
    def _setsp(req):
        try:
            data = req.json
            if "temperature" in data: setpoints["temperature"] = int(data["temperature"])
            if "humidity" in data:    setpoints["humidity"]    = int(data["humidity"])
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
            return devices
        except Exception as e:
            return { "error": str(e) }, 400
        
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
                status["temperature"] = sensor.d.temperature()
                status["humidity"] = sensor.d.humidity()
                status["last_sensor_ok_at"] = time.time()
                status["last_sensor_error"] = None

            now = time.time()
            last_ok = status["last_sensor_ok_at"]
            age = int(now - last_ok) if last_ok else None
            sensor_ok = (last_ok is not None) and (status["last_sensor_error"] is None)

            return {
                "ok": sensor_ok,   # “are readings currently healthy?”
                "sensors": {
                    "dht": {
                        "temperature": status["temperature"],
                        "humidity": status["humidity"],
                        "last_ok_age_s": age,
                        "last_error": status["last_sensor_error"],
                    }
                },
            }
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

    return app

async def start_server(cfg, wlan, io, sensor):
    app = make_app(cfg, wlan, io, sensor)
    await app.start_server(host='0.0.0.0', port=cfg["api_port"])

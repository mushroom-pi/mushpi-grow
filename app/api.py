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
        now = time.time()

        # Sensor availability judged by recency of a plausible read
        last_ok = status["last_sensor_ok_at"]
        age = int(now - last_ok) if last_ok else None
        max_age = max(2 * int(cfg["control"]["period_s"]), 10)
        sensor_ok = (age is not None) and (age <= max_age) and (status["last_sensor_error"] is None)

        # Availability from last /probe (with real feedback if present)
        last_probe_at = status.get("last_probe_at")
        last_probe_age = int(now - last_probe_at) if last_probe_at else None
        dev_av = status.get("devices_available") or {}

        # Compute outputs.available with 3-state logic (True/False/None)
        # - None if we have no evidence either way
        # - False if any device reported False
        # - True if at least one True and none False
        vals = [v for v in dev_av.values() if v is not None]
        if any(v is False for v in vals):
            outputs_ok = False
        elif any(v is True for v in vals):
            outputs_ok = True
        else:
            outputs_ok = None

        overall_ok = (sensor_ok and (outputs_ok is not False))

        return {
            "ok": overall_ok,
            "device": cfg["device_name"],

            "sensors": {
                "dht": {
                    "available": sensor_ok,
                    "last_ok_age_s": age,
                    "last_error": status["last_sensor_error"],
                }
            },

            "outputs": {
                "available": outputs_ok,
                "last_probe_age_s": last_probe_age,
                "devices": {
                    "fan": dev_av.get("fan"),
                    "humidifier": dev_av.get("humidifier"),
                    "heater": dev_av.get("heater"),
                },
            },

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

    @app.get('/devices')
    def _getdevices(req): return devices

    @app.post('/devices')
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
        
    @app.get('/devices/readings')
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
                "outputs": {
                    "devices": {
                        "fan": status["fan"],
                        "humidifier": status["humidifier"],
                        "heater": status["heater"],
                    }
                },
                "setpoints": {
                    "temperature": setpoints["temperature"],
                    "humidity": setpoints["humidity"],
                }
            }
        except Exception as e:
            status["last_sensor_error"] = str(e)
            return {"ok": False, "error": status["last_sensor_error"]}, 500

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


    @app.post('/probe')
    async def _probe(req):
        """
        Toggle each output briefly to check GPIO plumbing. Records availability for /health.
        """
        data = req.json or {}
        # if not data.get("allow_toggle"):
        #     return {"ok": False, "error": "allow_toggle required"}, 400
        pulse_ms = max(100, min(int(data.get("pulse_ms", 300)), 2000))

        async def pulse(pin):
            # observe before -> after, with a mid-check while asserted
            before = io.is_on(pin)
            ok1 = io.write(pin, True)
            mid = io.is_on(pin)
            await __import__("uasyncio").sleep_ms(pulse_ms)
            ok2 = io.write(pin, False)
            after = io.is_on(pin)
            toggled = ok1 and ok2 and (mid != before)  # changed when asserted
            return {
                "before": before, "mid": mid, "after": after,
                "pulse_ms": pulse_ms, "toggled": toggled
            }

        rep = {}
        rep["fan"] = await pulse(io.fan)
        rep["humidifier"] = await pulse(io.hum)
        include_heater = bool(data.get("include_heater", False))
        rep["heater"] = await pulse(io.heat) if include_heater else {"skipped": True, "toggled": None}

        # Persist a coarse availability view for /health (skipped -> None)
        status["last_probe_at"] = time.time()
        status["devices_available"] = {
            "fan": rep["fan"].get("toggled"),
            "humidifier": rep["humidifier"].get("toggled"),
            "heater": rep["heater"].get("toggled"),
        }

        return {"ok": True, "report": rep}

    return app

async def start_server(cfg, wlan, io, sensor):
    app = make_app(cfg, wlan, io, sensor)
    await app.start_server(host='0.0.0.0', port=cfg["api_port"])

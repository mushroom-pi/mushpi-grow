import time
from microdot import Microdot, Response

from .state import status, setpoints, boot_ts

def make_app(cfg, wlan, io):
    app = Microdot()
    Response.default_content_type = 'application/json'

    @app.get('/status')
    def _status(req): return status

    @app.get('/health')
    def _health(req):
        now = time.time()
        last_ok = status["last_sensor_ok_at"]
        age = int(now - last_ok) if last_ok else None
        return {
            "device": cfg["device_name"],
            "uptime_s": int(now - boot_ts),
            "wifi": {"connected": wlan.isconnected(), "ifconfig": wlan.ifconfig() if wlan.isconnected() else None},
            "sensor": {
                "ok": (status["temperature"] is not None and status["humidity"] is not None),
                "temp": status["temperature"], "humidity": status["humidity"],
                "last_ok_age_s": age, "last_error": status["last_sensor_error"],
            },
            "gpio": {"active_high": cfg["active_high"]},
        }

    @app.get('/setpoints')
    def _getsp(req):  return setpoints

    @app.post('/setpoints')
    def _setsp(req):
        try:
            data = req.json
            if "temperature" in data: setpoints["temperature"] = int(data["temperature"])
            if "humidity" in data:    setpoints["humidity"]    = int(data["humidity"])
            return {"ok": True, "setpoints": setpoints}
        except Exception as e:
            return {"ok": False, "error": str(e)}, 400

    @app.post('/probe')
    async def _probe(req):
        data = req.json or {}
        if not data.get("allow_toggle"): return {"ok": False, "error": "allow_toggle required"}, 400
        pulse_ms = max(100, min(int(data.get("pulse_ms", 300)), 2000))

        async def pulse(pin):
            before = io.is_on(pin); io.write(pin, True)
            await __import__("uasyncio").sleep_ms(pulse_ms)
            io.write(pin, False); after = io.is_on(pin)
            return {"before": before, "after": after, "pulse_ms": pulse_ms}

        rep = {}
        rep["fan"] = await pulse(io.fan)
        rep["humidifier"] = await pulse(io.hum)
        include_heater = bool(data.get("include_heater", False))
        rep["heater"] = await pulse(io.heat) if include_heater else {"skipped": True}
        return {"ok": True, "report": rep}

    return app

async def start_server(cfg, wlan, io):
    app = make_app(cfg, wlan, io)
    await app.start_server(host='0.0.0.0', port=cfg["api_port"])

import gc
import uasyncio as asyncio

from microdot import Microdot, Response, send_file

from .state import status, setpoints, devices, hub, get_system_info, _SOFTWARE_VERSION, _API_VERSION
from .metrics import system_snapshot
from .control import is_control_enabled, set_control_enabled, evaluate_hysteresis, reset_on_since, mark_relay_off, mark_relay_on, fault_latched
from .config_loader import save_config
from .config_validator import PIN_ROLES, VALID_GPIO_HINT, gpio_reject_reason, _append_duplicate_gpio_errors
from .shutdown import reboot, soft_reboot, stop_event
from . import uptime
from . import runtime_state

try:
    with open('app/provision.html', 'r') as f:
        _PROVISIONING_HTML = f.read()
except OSError:
    _PROVISIONING_HTML = '<html><body><h1>Mushroom Pi</h1><p>Error loading provisioning form.</p></body></html>'

try:
    with open('app/unit.html', 'r') as f:
        _UNIT_HTML = f.read()
except OSError:
    _UNIT_HTML = '<html><body><h1>Mushroom Pi</h1><p>Error loading unit page.</p></body></html>'

# Explicit Content-Type for the static route below. Keyed by the asset
# extension; the vendored Microdot would infer the same values from its
# own types_map, but naming them here keeps the firmware's contract with
# the browser independent of that gitignored library's internals.
# Anything unknown streams as application/octet-stream.
_STATIC_CONTENT_TYPES = {"js": "application/javascript", "svg": "image/svg+xml"}

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
        try:
            gc.collect()
        except:
            pass
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

    def _scheduled_reboot_info():
        """Build scheduled_reboot sub-object from cfg (config-derived, not runtime)."""
        reboot_cfg = cfg.get("reboot", {})
        return {
            "enabled": reboot_cfg.get("enabled", False),
            "hour": reboot_cfg.get("hour", 4),
            "minute": reboot_cfg.get("minute", 0),
        }

    def _uptime_with_reboot():
        """Return uptime info enriched with scheduled_reboot config."""
        info = uptime.get_uptime_info()
        info["scheduled_reboot"] = _scheduled_reboot_info()
        return info

    def _unit_page():
        """Render the on-device unit page. MODE is replaced BEFORE DEVNAME so
        a device_name that literally contains the token can never be
        re-scanned by a later pass (each .replace() only touches the string
        that exists at that moment)."""
        html = _UNIT_HTML.replace("MODE", mode).replace("DEVNAME", cfg.get("device_name", "pico"))
        return Response(html, status_code=200, headers={'Content-Type': 'text/html'})

    @app.get('/ui')
    def _ui(req):
        # Always HTML, in both modes: the offline (AP) variant is the same
        # status-and-control page with an offline banner and a link to the
        # Wi-Fi setup form — fully operable, pin remapping included; the
        # STA variant is the plain connected page.
        return _unit_page()

    @app.get('/static/<string:filename>')
    def _static(req, filename):
        # Page static assets (unit.js, favicon.svg) are streamed from flash
        # (1 KB chunks from Microdot's send_file) so they are browser-cacheable
        # and do not live in the heap as part of the page string. Content-Type
        # is passed explicitly from _STATIC_CONTENT_TYPES rather than left to
        # the vendored Microdot's inference. max_age is deliberately 0: this
        # Microdot has no ETag/304 support, so a cached copy cannot cheaply
        # revalidate — with a long max_age a browser would keep serving the
        # previous firmware's JS for an hour after a reflash. Re-fetching on
        # every load is cheap on the unit's own link. The `<string:...>`
        # segment type matches a single path component (no slashes), which
        # keeps a path-traversal shape out of the route entirely.
        try:
            ext = filename.rsplit('.', 1)[-1].lower()
            return send_file('app/static/' + filename,
                             content_type=_STATIC_CONTENT_TYPES.get(
                                 ext, 'application/octet-stream'),
                             max_age=0)
        except OSError:
            return {"ok": False, "error": "static asset missing"}, 404

    @app.get('/ping')
    def _pin(req):
        return

    @app.get('/health')
    def _health(req):
        snap = system_snapshot(wlan)
        snap["uptime"] = _uptime_with_reboot()
        return snap

    @app.get('/system')
    def system_info(req):
        info = get_system_info()
        info["uptime"] = _uptime_with_reboot()
        return info

    @app.get('/setpoints')
    def _getsp(req): return setpoints

    @app.post('/setpoints')
    def _setsp(req):
        try:
            data = req.json
            if "temperature" in data: setpoints["temperature"] = int(data["temperature"])
            if "humidity" in data:    setpoints["humidity"]    = int(data["humidity"])
            print("Updated setpoints to", setpoints)

            # Persist grower-chosen targets to runtime.json so they survive a
            # restart. A flash-write failure is logged but does NOT fail the
            # request — the response reports the live in-memory state (200).
            try:
                runtime_state.save_setpoints(setpoints["temperature"], setpoints["humidity"])
            except Exception as e:
                print("setpoints: persist failed:", e)

            # Immediate re-evaluation so relays react now rather than waiting
            # for the next control_loop tick. Only when control is enabled —
            # if disabled, the loop owns relay state (latched off) — and never
            # while the offline sensor-fault cut is latched, which would drive
            # relays from the very stale reading the cut is holding off on.
            if is_control_enabled() and not fault_latched():
                try:
                    evaluate_hysteresis(cfg, io)
                except Exception as e:
                    print("Immediate hysteresis eval failed:", e)

            return setpoints
        except Exception as e:
            return { "ok": False, "error": str(e)}, 400

    @app.get('/setup')
    def _getdevices(req): return devices

    @app.post('/setup')
    def _setdevices(req):
        try:
            data = req.json
            if "pins" in data:
                pins = data["pins"]
                if not isinstance(pins, dict):
                    return {"ok": False, "error": "pins must be a dict"}, 400
                for _k in PIN_ROLES:
                    if _k in pins:
                        _why = gpio_reject_reason(pins[_k])
                        if _why is not None:
                            return {"ok": False, "error": "pins." + _k + " - " + _why + " (" + VALID_GPIO_HINT + ")"}, 400
                # Reject updates that would leave two functions on one GPIO.
                # Checked against a merged view (current mapping overlaid with
                # this request's keys) BEFORE any mutation, so a rejected
                # request leaves GPIO state and flash completely unchanged.
                # Rules (mirrors the hub's setup-proxy validation):
                # - re-assigning a device to the pin it already has is a no-op
                #   and is allowed;
                # - a pin freed by another device in the same request is free;
                # - pre-existing duplicates this request does not touch pass —
                #   only conflicts the request creates or perpetuates fail.
                _changed = [k for k in ("dht", "humidifier", "fan", "heater") if k in pins]
                _merged = dict(devices["pins"])
                for _k in _changed:
                    _merged[_k] = pins[_k]
                for _k in _changed:
                    _v = pins[_k]
                    for _k2 in ("dht", "humidifier", "fan", "heater"):
                        if _k2 != _k and _merged.get(_k2) == _v:
                            return {"ok": False, "error": "GPIO " + str(_v) + " is already assigned to " + _k2}, 400
                # Finally run the boot duplicate rule itself over the merged
                # map: what this endpoint accepts must be a mapping the unit
                # would also boot with, so the guarantee is provable rather
                # than emergent. Same-pin re-assigns and within-request swaps
                # yield a distinct map, so they stay admitted as above.
                _errs = []
                _append_duplicate_gpio_errors(_merged, _errs)
                if _errs:
                    return {"ok": False, "error": _errs[0]}, 400

            # Apply active_high AFTER all pin validation so a rejected request
            # leaves RAM unchanged, but BEFORE the remap calls below —
            # io.remap() re-inits the pins and derives the off-level from
            # devices["active_high"], so the polarity must land first.
            if "active_high" in data:
                devices["active_high"] = bool(data["active_high"])

            if "pins" in data:
                if "dht" in pins: sensor.remap(pins)
                if "humidifier" in pins: io.remap("humidifier", pins)
                if "fan" in pins: io.remap("fan", pins)
                if "heater" in pins: io.remap("heater", pins)
                # io.remap() re-runs IO.__init__(), which resets _hb_task/
                # _hb_enabled: any live heartbeat or provisioning blink exits
                # and its finally turns the LED off. Re-arm the mode-
                # appropriate indicator — without this the offline double-blink
                # dies on save and the unit looks bricked until reboot.
                if mode == "ap":
                    io.start_provisioning_blink(stop_event=stop_event)
                else:
                    io.start_led_heartbeat(period_ms=800, stop_event=stop_event)

            # Persist to flash so GPIO mapping survives reboot
            cfg["pins"] = {k: devices["pins"][k] for k in ("dht", "humidifier", "fan", "heater")}
            cfg["active_high"] = devices["active_high"]
            try:
                save_config(cfg)
            except Exception as e:
                print("setup: persist failed:", e)
                return {"ok": False, "error": "persist failed: " + str(e), "devices": devices}, 500

            print("Updated devices to", devices)
            return devices
        except Exception as e:
            return {"ok": False, "error": str(e)}, 400

    @app.get('/provision')
    def _provision_form(req):
        # The Wi-Fi setup form, served in both operating modes at a stable
        # URL — the unit page's offline banner links here. Microdot keys its
        # routes by method, so this GET and the POST below register on the
        # same path without conflict. The POST stays mode-guarded: on a
        # connected (STA) unit the form loads but its submission is rejected
        # with 403 before any config write or reboot.
        html = _PROVISIONING_HTML.replace("DEVNAME", cfg.get("device_name", "pico"))
        return Response(html, status_code=200, headers={'Content-Type': 'text/html'})

    @app.post('/provision')
    def _provision(req):
        # Mode guard: offline (AP) operating mode only. Reject on a
        # connected unit BEFORE any save_config, config mutation, or reboot.
        if mode != "ap":
            return {"ok": False, "error": "provisioning is only available in AP mode"}, 403
        try:
            data = req.json
            wifi_data = data.get("wifi") if isinstance(data, dict) else None
            if not isinstance(wifi_data, dict):
                return {"ok": False, "error": "wifi object is required"}, 400
            ssid = wifi_data.get("ssid", "")
            if not ssid or not isinstance(ssid, str):
                return {"ok": False, "error": "wifi.ssid is required"}, 400
            cfg["wifi"]["ssid"] = ssid
            if "password" in wifi_data:
                cfg["wifi"]["password"] = wifi_data["password"]
            if "networks" in wifi_data:
                cfg["wifi"]["networks"] = wifi_data["networks"]
            try:
                save_config(cfg)
            except Exception as e:
                return {"ok": False, "error": str(e)}, 400
            print("WiFi credentials saved, rebooting in 2s...")
            asyncio.create_task(reboot(wlan=None, io=io, sensor=sensor, delay_ms=2000))
            return {"ok": True, "wifi": {"ssid": cfg["wifi"]["ssid"]}}
        except Exception as e:
            return {"ok": False, "error": str(e)}, 400

    @app.get('/sensors')
    def _status(req):
        """
        Returns readings + current device on/off state.
        NOTE: leaves the control loop as the source of truth for reads.
              If you want an on-demand fresh read, call with ?force=1
        """
        try:
            # Optional fresh read: /sensors?force=1
            if req.args.get('force'):
                sensor.read()

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
            valid_keys = ("humidifier", "fan", "heater")
            provided = {k: data[k] for k in valid_keys if k in data}
            if not provided:
                return ({ "ok": False, "error": 'the body must include at least one of: "humidifier", "fan", "heater"' }, 422)

            if "humidifier" in provided:
                humidifier = bool(provided["humidifier"])
                io.write(io.hum, humidifier); status["humidifier"] = humidifier
                if humidifier: mark_relay_on("humidifier")
                else: mark_relay_off("humidifier")
            if "fan" in provided:
                fan = bool(provided["fan"])
                io.write(io.fan, fan); status["fan"] = fan
                if fan: mark_relay_on("fan")
                else: mark_relay_off("fan")
            if "heater" in provided:
                heater = bool(provided["heater"])
                io.write(io.heat, heater); status["heater"] = heater
                if heater: mark_relay_on("heater")
                else: mark_relay_off("heater")

            # Persist the hand-set fan/humidifier positions so they survive a
            # restart while the loop stays off. Read from status (the applied
            # values), not the partial-body locals, so a single-key POST still
            # persists the full pair. The heater is never persisted. A
            # flash-write failure is logged but does NOT fail the request —
            # the 200 response reports the live state.
            try:
                runtime_state.save_outputs(status["fan"], status["humidifier"])
            except Exception as e:
                print("outputs: persist failed:", e)

            return { "ok": True, "fan": status["fan"], "humidifier": status["humidifier"], "heater": status["heater"] }
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
            return ({"ok": False, "error": 'send {"enabled": true|false}'}, 422)

        enabled = body["enabled"]
        set_control_enabled(enabled)

        # Persist the loop switch so the grower's choice survives a restart.
        # A flash-write failure is logged but does NOT fail the request.
        try:
            runtime_state.save_control_enabled(enabled)
        except Exception as e:
            print("control: persist failed:", e)

        # Optional: immediately force relays safe when disabling (LED heartbeat stays alive)
        if not enabled and hasattr(io, "relays_off"):
            try: io.relays_off()
            except: pass
            reset_on_since()

        return {"enabled": enabled}

    @app.post('/reboot')
    def handle_reboot(req):
        """POST /reboot — trigger a soft or hard reboot."""
        try:
            body = req.json
            reboot_type = body.get("type", "soft")
        except Exception:
            return {"ok": False, "error": "invalid JSON body"}, 400

        if reboot_type not in ("soft", "hard"):
            return {"ok": False, "error": "type must be 'soft' or 'hard'"}, 400

        # Mark the reboot type in uptime.json sentinel so init_uptime()
        # can classify it correctly on the next boot (reset_cause() on rp2
        # cannot distinguish intentional reboots from WDT resets).
        from . import uptime
        uptime.mark_pending_reboot(reboot_type)

        # Schedule a delayed reboot so the HTTP response has time to flush
        # before the Pico becomes unreachable. 2s is enough for the TCP
        # stack to transmit the response over WiFi.
        async def _delayed_reboot():
            await asyncio.sleep_ms(2000)
            if reboot_type == "soft":
                await soft_reboot(wlan, io, sensor, delay_ms=0)
            else:
                await reboot(wlan, io, sensor, delay_ms=0)

        asyncio.create_task(_delayed_reboot())

        return {"message": "Reboot initiated", "type": reboot_type}

    @app.get("/")
    def _get_all(req):
        # Content negotiation for the unit page, strict on purpose: HTML only
        # when the client asks for text/html AND does not mention
        # application/json at all. The hub's axios poll sends
        # "application/json, text/plain, */*" and must keep getting JSON;
        # browsers send text/html without application/json and get the page.
        # Microdot's Headers lookup is case-insensitive. The same in both
        # operating modes — the unit page is the landing page offline as well
        # as connected, and the Wi-Fi setup form has its own route
        # (GET /provision).
        accept = req.headers.get('Accept') or ''
        if 'text/html' in accept and 'application/json' not in accept:
            return _unit_page()
        return {
            "system": get_system_info(),
            "firmware_version": _SOFTWARE_VERSION,
            "api_version": _API_VERSION,
            "health": system_snapshot(wlan),
            "uptime": _uptime_with_reboot(),
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
            "control_loop_enabled": is_control_enabled(),
            # "configured" is read live from the boot config; "connected" is
            # the announce state written by app/announce.py (single writer) —
            # an ack of the most recent announce, never a live hub ping.
            "hub": {
                "configured": bool(cfg.get("hub_url")),
                "connected": hub["connected"],
            },
        }

    return app

async def start_server(cfg, wlan, io, sensor, mode="sta"):
    app = make_app(cfg, wlan, io, sensor, mode)
    await app.start_server(host='0.0.0.0', port=cfg["api_port"])

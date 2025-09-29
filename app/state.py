import utime as time

# global-ish dictionaries the whole app can import
setpoints = {"temperature": 25, "humidity": 60}
status = {
    "temperature": None, "humidity": None,
    "fan": False, "humidifier": False, "heater": False,
    "last_sensor_ok_at": None, "last_sensor_error": None,
}
boot_ms = time.ticks_ms()  # set at runtime
devices = {
    "pins": { "dht": 4, "humidifier": 6, "fan": 7, "heater": 8 },
    "active_high": False,
}
control_enabled = True

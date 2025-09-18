# global-ish dictionaries the whole app can import
setpoints = {"temperature": 25, "humidity": 60}
status = {
    "temperature": None, "humidity": None,
    "fan": False, "humidifier": False, "heater": False,
    "last_sensor_ok_at": None, "last_sensor_error": None,
    "last_probe_at": None,
    "devices_available": {"fan": None, "humidifier": None, "heater": None},
}
boot_ts = 0  # set at runtime

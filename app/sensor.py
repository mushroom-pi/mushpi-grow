import dht, time, machine

from .state import status, devices

class DHTReader:
    def __init__(self):
        self.d = dht.DHT11(machine.Pin(devices["pins"]["dht"]))

    def plausible(self, t, h):
        return (t is not None and h is not None
                and -10 <= t <= 60 and 0 <= h <= 100)

    def read(self):
        try:
            self.d.measure()
            t = self.d.temperature()
            h = self.d.humidity()
            print("Temperature: ", t)  # Print temperature
            print("Humidity: ", h)  # Print humidity
            status["temperature"] = t
            status["humidity"] = h
            if self.plausible(t, h):
                status["last_sensor_ok_at"] = time.time()
                status["last_sensor_error"] = None
            else:
                status["last_sensor_error"] = "implausible_reading"
        except Exception as e:
            status["last_sensor_error"] = str(e)

    def remap(self, pins):
        devices["pins"]["dht"] = int(pins["dht"])
        self.__init__()

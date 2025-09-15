import machine

class IO:
    def __init__(self, cfg):
        self.active_high = bool(cfg["active_high"])
        pins = cfg["pins"]
        self.dht_pin = machine.Pin(pins["dht"])
        self.fan = machine.Pin(pins["fan"], machine.Pin.OUT, value=self._off())
        self.hum = machine.Pin(pins["humid"], machine.Pin.OUT, value=self._off())
        self.heat = machine.Pin(pins["heater"], machine.Pin.OUT, value=self._off())

    def _on(self):  return 1 if self.active_high else 0
    def _off(self): return 0 if self.active_high else 1

    def write(self, pin, is_on):
        try:
            pin.value(self._on() if is_on else self._off())
            return True
        except:
            return False

    def is_on(self, pin):
        try:
            return pin.value() == self._on()
        except:
            return False

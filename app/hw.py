from machine import Pin
import utime as time

from .state import devices

class IO:
    def __init__(self):
        pins = devices["pins"]
        self.dht_pin = Pin(pins["dht"])
        self.hum = Pin(pins["humidifier"], Pin.OUT, value=self._off())
        self.fan = Pin(pins["fan"], Pin.OUT, value=self._off())
        self.heat = Pin(pins["heater"], Pin.OUT, value=self._off())
        self.led_onboard = Pin('LED', Pin.OUT, value=self._off())

    def _on(self):  return 1 if devices["active_high"] else 0
    def _off(self): return 0 if devices["active_high"] else 1

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
        
    def remap(self, device, pins):
        devices["pins"][device] = int(pins[device])
        self.__init__()
        
    def led_on(self):
        self.led_onboard.value(1)

    def led_off(self):
        self.led_onboard.value(0)

    def led_blink(self, time_ms = 200):
        time_s = time_ms / 1000
        while True:
            self.led_on()
            time.sleep(time_s)
            self.lef_off()
            time.sleep(time_s)

    def all_off(self):
        self.led_off()
        self.hum(self._off())
        self.fan(self._off())
        self.heat(self._off())

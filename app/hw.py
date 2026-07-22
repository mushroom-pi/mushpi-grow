from machine import Pin
import utime as time
import uasyncio as asyncio

from .state import devices, status

class IO:
    def __init__(self):
        pins = devices["pins"]
        self.dht_pin = Pin(pins["dht"])
        self.hum = Pin(pins["humidifier"], Pin.OUT, value=self._off())
        self.fan = Pin(pins["fan"], Pin.OUT, value=self._off())
        self.heat = Pin(pins["heater"], Pin.OUT, value=self._off())
        self.led_onboard = Pin('LED', Pin.OUT, value=self._off())
        self._hb_task = None
        self._hb_enabled = False
        self._hb_period_ms = 1000
        self.led_on()

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
        
    def led_on(self): self.led_onboard.value(1)

    def led_off(self): self.led_onboard.value(0)

    def start_led_heartbeat(self, period_ms=1000, stop_event=None):
        """Start a continuous, non-blocking blink. Call from within the event loop."""
        # cancel existing heartbeat if any
        if self._hb_task:
            try: self._hb_task.cancel()
            except: pass
            self._hb_task = None

        self._hb_enabled = True
        self._hb_period_ms = max(2, int(period_ms))
        # schedule background task
        self._hb_task = asyncio.create_task(self._heartbeat_loop(stop_event))

    def set_led_heartbeat_period(self, period_ms):
        """Change heartbeat speed on the fly."""
        self._hb_period_ms = max(2, int(period_ms))

    def stop_led_heartbeat(self):
        """Stop the heartbeat and turn LED off."""
        self._hb_enabled = False
        if self._hb_task:
            try: self._hb_task.cancel()
            except: pass
            self._hb_task = None
        self.led_onboard.value(0)

    stop_provisioning_blink = stop_led_heartbeat

    async def _heartbeat_loop(self, stop_event):
        try:
            while self._hb_enabled and not (stop_event and stop_event.is_set()):
                half = max(1, self._hb_period_ms // 2)
                self.led_onboard.value(1); await asyncio.sleep_ms(half)
                self.led_onboard.value(0); await asyncio.sleep_ms(half)
        finally:
            self.led_onboard.value(0)
            self._hb_task = None

    def start_provisioning_blink(self, stop_event=None):
        """Asymmetric blink: 200 on / 200 off / 200 on / 800 off — provisioning indicator."""
        # cancel any existing heartbeat task first
        if self._hb_task:
            try: self._hb_task.cancel()
            except: pass
            self._hb_task = None

        self._hb_enabled = True
        self._hb_task = asyncio.create_task(self._provisioning_blink_loop(stop_event))

    async def _provisioning_blink_loop(self, stop_event):
        try:
            while self._hb_enabled and not (stop_event and stop_event.is_set()):
                # 200ms ON
                self.led_onboard.value(1); await asyncio.sleep_ms(200)
                # 200ms OFF
                self.led_onboard.value(0); await asyncio.sleep_ms(200)
                # 200ms ON
                self.led_onboard.value(1); await asyncio.sleep_ms(200)
                # 800ms OFF
                self.led_onboard.value(0); await asyncio.sleep_ms(800)
        finally:
            self.led_onboard.value(0)
            self._hb_task = None

    # Optional: short, non-blocking flash for events (announce ok, etc.)
    def led_flash(self, cycles=2, period_ms=120):
        asyncio.create_task(self._flash_worker(cycles, period_ms))

    async def _flash_worker(self, cycles, period_ms):
        half = max(1, period_ms // 2)
        for _ in range(cycles):
            self.led_onboard.value(1); await asyncio.sleep_ms(half)
            self.led_onboard.value(0); await asyncio.sleep_ms(half)

    def led_solid(self, on=True):
        """Force LED steady state (cancels heartbeat)."""
        self.stop_led_heartbeat()
        self.led_onboard.value(1 if on else 0)

    
    def error_blink_blocking(self, blinks=3, on_ms=150, off_ms=150, pause_ms=1000):
        """Terminal fatal-state blink. Blocking, never returns.
        Use only when the device cannot continue (e.g. config validation
        failed before the event loop starts)."""
        while True:
            for _ in range(blinks):
                self.led_onboard.value(1)
                time.sleep_ms(on_ms)
                self.led_onboard.value(0)
                time.sleep_ms(off_ms)
            time.sleep_ms(pause_ms)

    def hum_on(self):
        self.hum(self._on())
        status["humidifier"] = True

    def hum_off(self):
        self.hum(self._off())
        status["humidifier"] = False

    def fan_on(self):
        self.fan(self._on())
        status["fan"] = True

    def fan_off(self):
        self.fan(self._off())
        status["fan"] = False

    def heat_on(self):
        self.heat(self._on())
        status["heater"] = True

    def heat_off(self):
        self.heat(self._off())
        status["heater"] = False

    def relays_off(self):
        """Turn off relays only; keep LED heartbeat running."""
        self.hum_off()
        self.fan_off()
        self.heat_off()

    def all_off(self):
        self.led_off()
        self.relays_off()
        self.stop_led_heartbeat()


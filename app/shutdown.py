import uasyncio as asyncio

# Global event you can pass to your loops to tell them to exit
stop_event = asyncio.Event()

def request_shutdown():
    """Call this to ask all tasks to stop (from an endpoint, button, etc.)."""
    stop_event.set()

async def graceful_shutdown(wlan=None, io=None, sensor=None):
    """Best-effort cleanup that runs when the app is stopping."""
    # Let tasks notice the stop and finish their iteration
    stop_event.set()
    await asyncio.sleep_ms(0)

    # Turn outputs OFF (fail-safe)
    try:
        if io and hasattr(io, "all_off"):
            io.all_off()
        elif io and hasattr(io, "safe_off"):
            io.safe_off()
    except Exception as e:
        print("io cleanup error:", e)

    # Deinit sensor if it supports it
    try:
        if sensor and hasattr(sensor, "deinit"):
            sensor.deinit()
    except Exception as e:
        print("sensor cleanup error:", e)

    # Bring Wi-Fi down to save power and close sockets
    try:
        if wlan:
            if hasattr(wlan, "isconnected") and wlan.isconnected():
                wlan.disconnect()
            if hasattr(wlan, "active"):
                wlan.active(False)
    except Exception as e:
        print("wifi cleanup error:", e)

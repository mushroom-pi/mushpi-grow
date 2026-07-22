import time
import uasyncio as asyncio
import ntptime
import machine


_reboot_in_progress = False

# Retry NTP every 5 minutes if initial sync fails
_NTP_RETRY_MS = 5 * 60 * 1000


async def sync_ntp_once(host, tz_offset_hours):
    """Attempt NTP sync. Returns True on success. Best-effort."""
    try:
        ntptime.host = host
        ntptime.settime()
        # ntptime.settime() sets the RTC to UTC; apply timezone offset
        if tz_offset_hours != 0:
            rtc = machine.RTC()
            yr, mo, dy, wd, hr, mi, se, sub = rtc.datetime()
            hr += tz_offset_hours
            # Handle day rollover
            if hr >= 24:
                hr -= 24
                dy += 1
            elif hr < 0:
                hr += 24
                dy -= 1
            rtc.datetime((yr, mo, dy, wd, hr, mi, se, sub))
        print("ntp: synced to", host)
        return True
    except Exception as e:
        print("ntp: sync failed -", e)
        return False


async def reboot_scheduler_loop(cfg, wlan, io, sensor, stop_event=None):
    """Daily scheduled soft-reboot coroutine. Spawned in STA mode only."""
    global _reboot_in_progress

    reboot_cfg = cfg.get("reboot", {})
    if not reboot_cfg.get("enabled", False):
        print("reboot_scheduler: disabled in config, exiting")
        return

    target_hour = reboot_cfg.get("hour", 4)
    target_minute = reboot_cfg.get("minute", 0)
    ntp_host = reboot_cfg.get("ntp_host", "pool.ntp.org")
    tz_offset = reboot_cfg.get("ntp_tz_offset_hours", 0)

    # Initial bootstrap: try NTP 3× quickly at boot
    synced = False
    for _ in range(3):
        if stop_event and stop_event.is_set():
            return
        ok = await sync_ntp_once(ntp_host, tz_offset)
        if ok:
            synced = True
            break
        await asyncio.sleep_ms(30000)

    if not synced:
        print("reboot_scheduler: NTP sync failed after 3 attempts,"
              " will retry every 5 min until synced")

    # Lazy import to avoid circular issues at boot
    from . import uptime

    last_ntp_retry = time.ticks_ms()
    poll_chunks = 300  # 60 s in 200ms chunks

    if synced:
        print("reboot_scheduler: active -- target %02d:%02d (tz=%+d)" %
              (target_hour, target_minute, tz_offset))
    else:
        print("reboot_scheduler: active -- waiting for NTP sync before"
              " scheduling reboots at %02d:%02d" % (target_hour, target_minute))

    while True:
        if stop_event and stop_event.is_set():
            return

        # Periodic NTP retry if initial sync failed
        if not synced:
            now = time.ticks_ms()
            if time.ticks_diff(now, last_ntp_retry) >= _NTP_RETRY_MS:
                synced = await sync_ntp_once(ntp_host, tz_offset)
                last_ntp_retry = now
                if synced:
                    print("reboot_scheduler: NTP recovered,"
                          " scheduling reboots at %02d:%02d" %
                          (target_hour, target_minute))

        if _reboot_in_progress:
            await asyncio.sleep_ms(60000)
            continue

        # Only check time if NTP has synced (otherwise RTC is garbage)
        if synced:
            try:
                t = time.localtime()
                now_hour = t[3]
                now_minute = t[4]
                today = uptime.today_str()
                done_date = uptime.reboot_done_date()
            except Exception as e:
                print("reboot_scheduler: time read error -", e)
                synced = False  # RTC may be corrupted, force re-sync
                await asyncio.sleep_ms(60000)
                continue

            if (now_hour == target_hour and now_minute == target_minute
                    and today and today != done_date
                    and not _reboot_in_progress):
                _reboot_in_progress = True
                print("reboot_scheduler: scheduled reboot triggered"
                      " at %02d:%02d" % (now_hour, now_minute))

                # Mark this as a scheduled reboot in uptime.json sentinel
                # (reset_cause() on rp2 cannot distinguish soft_reset from WDT)
                uptime.mark_pending_reboot("scheduled")

                # Import and call soft_reboot from shutdown
                from .shutdown import soft_reboot
                await soft_reboot(wlan, io, sensor, delay_ms=0)
                # soft_reboot never returns (calls machine.soft_reset())

        # Sleep 60s in 200ms chunks for responsive shutdown
        for _ in range(poll_chunks):
            if stop_event and stop_event.is_set():
                return
            await asyncio.sleep_ms(200)

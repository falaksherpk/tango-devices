#!/usr/bin/env python3
"""
Part 3 Chapter 2 -- subscribe to MagnetPowerSupply's real Tango
change_event on 'current', proving push_change_event() genuinely
delivers pushed updates during a ramp, with no polling.
"""
import time
import tango


def on_current_event(event):
    if event.err:
        print(f"[subscriber] EVENT ERROR: {event.errors}")
        return
    value = event.attr_value.value
    timestamp = event.attr_value.time.todatetime()
    print(f"[subscriber] change_event: current={value:.4f} A  ({timestamp})")


def main():
    dp = tango.DeviceProxy("linac/magnet/q1")
    event_id = dp.subscribe_event(
        "current", tango.EventType.CHANGE_EVENT, on_current_event, stateless=True
    )
    print("[subscriber] subscribed, waiting for pushed events (Ctrl-C to stop)...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dp.unsubscribe_event(event_id)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Lab 3.1, Step 3 -- subscribe to TemperatureSensor's real Tango
change_event, proving Tango's own event system (built on ZMQ under
the hood since Tango 9) delivers pushed updates with no polling.
"""
import time
import tango


def on_temperature_event(event):
    if event.err:
        print(f"[subscriber] EVENT ERROR: {event.errors}")
        return
    value = event.attr_value.value
    timestamp = event.attr_value.time.todatetime()
    print(
        f"[subscriber] change_event received: Temperature={value:.3f}  "
        f"(device timestamp {timestamp})"
    )


def main():
    dp = tango.DeviceProxy("test/temperature/1")

    # stateless=True: subscribe even if the device is briefly
    # unavailable at subscribe time, and keep retrying in the
    # background -- more robust than stateless=False for a real
    # client, though not essential for this lab's localhost setup.
    event_id = dp.subscribe_event(
        "Temperature",
        tango.EventType.CHANGE_EVENT,
        on_temperature_event,
        stateless=True,
    )
    print("[subscriber] subscribed, waiting for pushed events (Ctrl-C to stop)...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[subscriber] unsubscribing...")
        dp.unsubscribe_event(event_id)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Lab 3.3 -- subscribe to ModbusVacuumController's VacuumPressure
change_event, to verify it fires on real pressure changes and does
NOT fire every second while pressure is flat (pump off).
"""
import time
import tango


def on_pressure_event(event):
    if event.err:
        print(f"[event-subscriber] EVENT ERROR: {event.errors}")
        return
    value = event.attr_value.value
    timestamp = event.attr_value.time.todatetime()
    print(
        f"[event-subscriber] change_event: VacuumPressure={value:.4f} mbar  "
        f"({timestamp})"
    )


def main():
    dp = tango.DeviceProxy("test/vacuum/1")
    event_id = dp.subscribe_event(
        "VacuumPressure",
        tango.EventType.CHANGE_EVENT,
        on_pressure_event,
        stateless=True,
    )
    print(
        "[event-subscriber] subscribed, waiting for pushed events (Ctrl-C to stop)..."
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dp.unsubscribe_event(event_id)


if __name__ == "__main__":
    main()

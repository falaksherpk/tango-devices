import time
import random
import threading
from tango import DevState
from tango.server import Device, attribute, command, run


class TemperatureSensor(Device):
    """A simulated temperature sensor with alarm thresholds, a derived
    Pressure attribute, and (new, Lab 3.1) push-based change events on
    Temperature -- demonstrating Tango's own event system, which is
    built on ZMQ under the hood since Tango 9."""

    ALARM_MIN = 19.0
    ALARM_MAX = 21.0

    def init_device(self):
        super().init_device()
        self._temperature = 20.0
        self.set_state(DevState.ON)

        # Lab 3.1: declare that we will manually push change events for
        # Temperature (True = implemented by us), and tell Tango not to
        # also apply its own automatic change-detection on top (False =
        # detect off) -- we decide explicitly, in code, when to push.
        self.set_change_event("Temperature", True, False)

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

    def _update_loop(self):
        while not self._stop_event.is_set():
            self._temperature += random.uniform(-0.5, 0.5)
            # push_change_event() is safe to call from a background
            # thread outside the normal attribute-access path -- this
            # is the standard PyTango pattern for background-driven
            # attributes that need to notify subscribers as soon as a
            # new value is available, not just whenever a client next
            # happens to poll.
            self.push_change_event("Temperature", self._temperature)
            time.sleep(1)

    def always_executed_hook(self):
        if self._temperature < self.ALARM_MIN or self._temperature > self.ALARM_MAX:
            self.set_state(DevState.ALARM)
        else:
            self.set_state(DevState.ON)

    @attribute(
        dtype=float,
        label="Temperature",
        unit="degC",
        min_alarm=ALARM_MIN,
        max_alarm=ALARM_MAX,
    )
    def Temperature(self):
        return self._temperature

    @attribute(dtype=float, label="Pressure", unit="bar")
    def Pressure(self):
        return round(1.0 + (self._temperature - 20.0) * 0.05, 4)

    @command
    def Reset(self):
        self._temperature = 20.0
        # Push immediately, so a subscriber sees the reset right away
        # rather than waiting up to 1s for the next background tick.
        self.push_change_event("Temperature", self._temperature)

    def delete_device(self):
        self._stop_event.set()
        super().delete_device()


if __name__ == "__main__":
    run((TemperatureSensor,))

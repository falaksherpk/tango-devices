import time
import random
import threading

from tango import DevState
from tango.server import Device, attribute, command, run


class TemperatureSensor(Device):
    """A simulated temperature sensor with alarm thresholds and a derived Pressure attribute."""

    ALARM_MIN = 19.0
    ALARM_MAX = 21.0

    def init_device(self):
        super().init_device()
        self._temperature = 20.0
        self.set_state(DevState.ON)

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

    def _update_loop(self):
        while not self._stop_event.is_set():
            self._temperature += random.uniform(-0.5, 0.5)
            time.sleep(1)

    def always_executed_hook(self):
        # Tango calls this before every attribute/command access -- the
        # idiomatic place to keep device State in sync with current values,
        # rather than managing State from inside the background thread.
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
        # Toy derived relation: pressure rises/falls with temperature deviation
        # from a 20 degC baseline. Not physically rigorous -- the point is
        # demonstrating a multi-attribute device where one value depends on
        # another, computed fresh on every read rather than tracked separately.
        return round(1.0 + (self._temperature - 20.0) * 0.05, 4)

    @command
    def Reset(self):
        self._temperature = 20.0

    def delete_device(self):
        self._stop_event.set()
        super().delete_device()


if __name__ == "__main__":
    run((TemperatureSensor,))

from tango import DevState
from tango.server import Device, attribute, command, run


class PowerSupply(Device):
    """A simple simulated power supply: settable Voltage, derived Current, On/Off."""

    RESISTANCE_OHMS = 10.0

    def init_device(self):
        super().init_device()
        self._voltage = 0.0
        self._output = False
        self.set_state(DevState.OFF)

    @attribute(dtype=float, label="Voltage", unit="V")
    def Voltage(self):
        return self._voltage

    @Voltage.setter
    def Voltage(self, value):
        self._voltage = value

    @attribute(dtype=float, label="Current", unit="A")
    def Current(self):
        # Toy resistive-load model: I = V / R, only when output is enabled.
        return self._voltage / self.RESISTANCE_OHMS if self._output else 0.0

    @attribute(dtype=bool, label="Output")
    def Output(self):
        return self._output

    @command
    def On(self):
        self._output = True
        self.set_state(DevState.ON)

    @command
    def Off(self):
        self._output = False
        self.set_state(DevState.OFF)


if __name__ == "__main__":
    run((PowerSupply,))

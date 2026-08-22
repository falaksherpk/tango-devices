import tango
from tango import DevState
from tango.server import Device, attribute, command, run


class MagnetPowerSupply(Device):
    """A magnet controller that commands an underlying PowerSupply device
    via DeviceProxy, rather than modeling its own hardware directly --
    mirrors real accelerator magnet PSU architecture."""

    RESISTANCE_OHMS = 10.0  # must match the target PowerSupply's own model

    def init_device(self):
        super().init_device()
        self._psu = tango.DeviceProxy("test/powersupply/1")
        self.set_state(DevState.OFF)

    @attribute(dtype=float, label="MagnetCurrent", unit="A")
    def MagnetCurrent(self):
        # Read straight through to the underlying PSU's own Current --
        # this device doesn't track its own separate state, it reflects
        # whatever the real supply is actually doing right now.
        return self._psu.Current

    @MagnetCurrent.setter
    def MagnetCurrent(self, target_current):
        # Convert the desired current into the voltage the PSU needs to
        # produce it (V = I * R), then command the PSU directly.
        self._psu.Voltage = target_current * self.RESISTANCE_OHMS

    @command
    def MagnetOn(self):
        self._psu.On()
        self.set_state(DevState.ON)

    @command
    def MagnetOff(self):
        self._psu.Off()
        self.set_state(DevState.OFF)


if __name__ == "__main__":
    run((MagnetPowerSupply,))

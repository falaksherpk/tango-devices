"""
Part 3 Chapter 7 -- Sardana ZeroDController wrapping
linac/beam/faradaycup1 (Ch4's Faraday cup / picoammeter).

Design decision (LAB 7.1): ZeroDController, not CounterTimerController.
The Faraday cup's `current` is a continuously-available scalar with no
real hardware-timed acquisition lifecycle (no genuine LoadOne/StartOne
integration window exists in the underlying simulated instrument) --
matching Sardana's own real ZeroD example (a CPU load-average reader),
not a triggered/timed counter. Forcing CounterTimerController's
Start/Load semantics onto a value that is simply always available
would mean faking an acquisition delay that doesn't reflect anything
real about this device.

Real attribute mapping, verified against linac/beam/faradaycup1's
actual source (not assumed):
    ReadOne(axis)  -> reads the device's current
    StateOne(axis) -> maps the device's real DevState
"""
import tango
from sardana import State
from sardana.pool.controller import ZeroDController


class FaradayZeroDController(ZeroDController):
    MaxDevice = 1

    ctrl_properties = {
        "TangoDevice": {
            "Type": str,
            "Description": "Tango device name of the Faraday cup",
            "DefaultValue": "linac/beam/faradaycup1",
        },
    }

    STATE_MAP = {
        tango.DevState.ON: State.On,
        tango.DevState.FAULT: State.Fault,
    }

    def __init__(self, inst, props, *args, **kwargs):
        super().__init__(inst, props, *args, **kwargs)
        self.proxy = None

    def AddDevice(self, _axis):
        if self.proxy is None:
            self.proxy = tango.DeviceProxy(self.TangoDevice)

    def DeleteDevice(self, _axis):
        self.proxy = None

    def StateOne(self, _axis):
        dev_state = self.proxy.state()
        state = self.STATE_MAP.get(dev_state, State.Fault)
        status = self.proxy.status()
        return state, status

    def ReadOne(self, _axis):
        return self.proxy.current

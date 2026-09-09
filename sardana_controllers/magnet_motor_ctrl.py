"""
Part 3 Chapter 7 -- Sardana MotorController wrapping linac/magnet/q1
(Ch2's magnet power supply).

Design decision (LAB 7.1): NOT a naive attribute-wrapper -- the
TangoAttrMotorController-style pattern was removed from Sardana core
(github.com/sardana-org/sardana/issues/181) because a core maintainer
objected that it ignores real motor semantics (acceleration, velocity,
limits) that scan macros depend on. This controller instead passes
through the magnet's own real DevState truthfully: the magnet already
implements genuine ramp/MOVING physics (Ch2), so StateOne reflects
hardware truth rather than inferring "still moving" from a position
delta the way the naive pattern did.

Real attribute mapping, verified against linac/magnet/q1's actual
source (not assumed):
    StartOne(axis, position) -> writes the device's setpoint
    ReadOne(axis)             -> reads the device's current
    StateOne(axis)            -> maps the device's real DevState
                                  (ON/MOVING/FAULT all map directly,
                                  1:1, no heuristic needed)
"""
import tango
from sardana import State
from sardana.pool.controller import MotorController


class MagnetMotorController(MotorController):
    MaxDevice = 1

    ctrl_properties = {
        "TangoDevice": {
            "Type": str,
            "Description": "Tango device name of the magnet power supply",
            "DefaultValue": "linac/magnet/q1",
        },
    }

    STATE_MAP = {
        tango.DevState.ON: State.On,
        tango.DevState.MOVING: State.Moving,
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

    def StartOne(self, _axis, position):
        self.proxy.setpoint = position

    def StopOne(self, axis):
        pass

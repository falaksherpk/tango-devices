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

LAB 7.9 compliance audit finding: AddDevice does not fail even when
the wrapped device is completely unreachable (not running/not
exported), since tango.DeviceProxy() construction is lazy -- the
failure previously only surfaced as an unhandled tango.DevFailed on
the first real call. Confirmed via manual test: killing
faraday_cup.py entirely left AddDevice silently succeeding, with
StateOne raising a raw ConnectionFailed instead of returning
something distinguishable. StateOne now catches tango.DevFailed and
maps it to State.Fault with a status string that names the real
underlying reason, consistent with how a live device reporting its
own FAULT state (e.g. backend instrument lost) already surfaces a
real, informative status.
"""
import tango
from sardana import State
from sardana.pool.controller import ZeroDController


class FaradayZeroDController(ZeroDController):
    """Sardana ZeroD element wrapping linac/beam/faradaycup1's always-on
    current reading -- see module docstring for why ZeroD, not
    CounterTimer."""

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
        try:
            dev_state = self.proxy.state()
            status = self.proxy.status()
        except tango.DevFailed as e:
            reason = e.args[0].desc if e.args else str(e)
            return State.Fault, f"Wrapped device unreachable: {reason}"
        state = self.STATE_MAP.get(dev_state, State.Fault)
        return state, status

    def ReadOne(self, _axis):
        return self.proxy.current

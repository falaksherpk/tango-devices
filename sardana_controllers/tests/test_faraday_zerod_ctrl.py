"""
LAB 7.8 -- controller-unit tests for the Sardana ZeroDController wrapping
linac/beam/faradaycup1 (Ch4's Faraday cup / picoammeter).

Formalizes the pattern proven in sardana_controllers/lab72_proof.py: direct
instantiation, no Pool involved, against the real, already-running device.
Read-only wrapper -- no StartOne/ramping exists on this controller, so no
position-restoring teardown is needed (unlike the magnet).
"""
from sardana import State

from sardana_controllers.faraday_zerod_ctrl import FaradayZeroDController


def make_ctrl():
    ctrl = FaradayZeroDController(
        "test_inst", {"TangoDevice": "linac/beam/faradaycup1"}
    )
    ctrl.AddDevice(1)
    return ctrl


def test_initial_state_is_on():
    ctrl = make_ctrl()
    state, status = ctrl.StateOne(1)
    assert state == State.On
    assert status
    ctrl.DeleteDevice(1)


def test_read_one_returns_positive_float():
    ctrl = make_ctrl()
    reading = ctrl.ReadOne(1)
    assert isinstance(reading, float)
    assert reading > 0
    ctrl.DeleteDevice(1)


def test_read_one_is_repeatable():
    ctrl = make_ctrl()
    first = ctrl.ReadOne(1)
    second = ctrl.ReadOne(1)
    assert isinstance(first, float)
    assert isinstance(second, float)
    ctrl.DeleteDevice(1)

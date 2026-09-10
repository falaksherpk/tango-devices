"""
LAB 7.8 -- controller-unit tests for the Sardana MotorController wrapping
linac/magnet/q1 (Ch2's magnet power supply).

Formalizes the pattern already proven in sardana_controllers/lab72_proof.py:
direct instantiation, no Pool involved, against the real, already-running
device. Teardown restores the magnet's original position since this drives
real hardware state, not a mock.
"""
import time

import pytest
from sardana import State

from sardana_controllers.magnet_motor_ctrl import MagnetMotorController


@pytest.fixture
def magnet_ctrl():
    ctrl = MagnetMotorController("test_inst", {"TangoDevice": "linac/magnet/q1"})
    ctrl.AddDevice(1)
    original_position = ctrl.ReadOne(1)

    yield ctrl

    # Restore original position -- real hardware state, not a mock.
    ctrl.StartOne(1, original_position)
    deadline = time.time() + 10
    while time.time() < deadline:
        state, _ = ctrl.StateOne(1)
        if state != State.Moving:
            break
        time.sleep(0.2)
    ctrl.DeleteDevice(1)


def test_initial_state_is_on_or_moving(magnet_ctrl):
    state, status = magnet_ctrl.StateOne(1)
    assert state in (State.On, State.Moving)
    assert status


def test_read_one_returns_float(magnet_ctrl):
    position = magnet_ctrl.ReadOne(1)
    assert isinstance(position, float)


def test_start_one_ramps_to_target(magnet_ctrl):
    start_position = magnet_ctrl.ReadOne(1)
    target = start_position - 2.0  # move down; lab72_proof.py moved up

    magnet_ctrl.StartOne(1, target)

    deadline = time.time() + 10
    while time.time() < deadline:
        state, _ = magnet_ctrl.StateOne(1)
        if state != State.Moving:
            break
        time.sleep(0.2)

    final_state, _ = magnet_ctrl.StateOne(1)
    final_position = magnet_ctrl.ReadOne(1)
    assert final_state == State.On
    assert abs(final_position - target) < 0.01

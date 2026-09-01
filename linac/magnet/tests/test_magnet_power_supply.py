"""
Part 3 Chapter 2 -- DeviceTestContext pytest suite for MagnetPowerSupply.

Mocks hardware and Redis, not the ramp logic itself -- the point is
testing the real GreenMode.Asyncio ramp physics (state transitions,
threshold-gated archiving calls) against a deterministic fake
"instrument" rather than real serial I/O or a real Redis server.

Real issue found building this: DeviceTestContext(process=False) --
running the device server in the SAME process as the test -- segfaults
under this device specifically, when the test polls state()/current in
a tight loop while the device's own asyncio ramp task is concurrently
running. Fixed by using process=True (a real subprocess), which is
also closer to how a real device server actually runs in production.
"""
import time
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from tango import DevState
from tango.test_context import DeviceTestContext

import magnet_power_supply


class FakeHardware:
    """In-memory stand-in for the real RS-232 magnet PSU."""
    def __init__(self, initial=0.0):
        self.current = initial


@pytest.fixture
def device():
    hw = FakeHardware(initial=0.0)

    # `self` is required and unused: these functions replace bound methods on
    # MagnetPowerSupply via patch.object, so their signature must match the
    # original instance methods they stand in for.
    async def fake_query_current(self):  # noqa: ARG001
        return hw.current

    async def fake_write_current(self, value):  # noqa: ARG001
        hw.current = value

    with patch.object(magnet_power_supply, "serial") as mock_serial_module:
        mock_serial_module.Serial.return_value = MagicMock()
        with (
            patch.object(
                magnet_power_supply.MagnetPowerSupply,
                "_query_current",
                fake_query_current,
            ),
            patch.object(
                magnet_power_supply.MagnetPowerSupply,
                "_write_current",
                fake_write_current,
            ),
            patch.object(
                magnet_power_supply.redis, "Redis", return_value=AsyncMock()
            ),
            DeviceTestContext(
                magnet_power_supply.MagnetPowerSupply,
                # fast ramp -- tests shouldn't wait on real-world timing
                properties={"ramp_rate": 50.0},
                process=True,  # required for GreenMode.Asyncio -- see module docstring
            ) as proxy,
        ):
            yield proxy


def test_initial_state_is_on(device):
    assert device.state() == DevState.ON


def test_initial_current_and_setpoint_are_zero(device):
    assert device.current == pytest.approx(0.0)
    assert device.setpoint == pytest.approx(0.0)


def test_writing_setpoint_triggers_moving_state(device):
    device.setpoint = 10.0
    deadline = time.time() + 2
    seen_moving = False
    while time.time() < deadline:
        if device.state() == DevState.MOVING:
            seen_moving = True
            break
        time.sleep(0.02)
    assert seen_moving, "device never entered MOVING state after a setpoint write"


def test_ramp_settles_at_target_and_returns_to_on(device):
    device.setpoint = 5.0
    deadline = time.time() + 3
    while time.time() < deadline:
        if device.state() == DevState.ON and device.current == pytest.approx(
            5.0, abs=1e-3
        ):
            break
        time.sleep(0.02)
    assert device.state() == DevState.ON
    assert device.current == pytest.approx(5.0, abs=1e-3)


def test_reset_ramps_back_to_zero(device):
    device.setpoint = 8.0
    deadline = time.time() + 3
    while time.time() < deadline and device.state() != DevState.ON:
        time.sleep(0.02)

    device.Reset()
    deadline = time.time() + 3
    while time.time() < deadline:
        if device.state() == DevState.ON and device.current == pytest.approx(
            0.0, abs=1e-3
        ):
            break
        time.sleep(0.02)
    assert device.current == pytest.approx(0.0, abs=1e-3)


def test_current_alarm_bounds_are_set(device):
    config = device.get_attribute_config("current")
    assert float(config.min_alarm) == -1.0
    assert float(config.max_alarm) == 100.0

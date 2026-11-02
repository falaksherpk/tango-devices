"""
Part 3 Chapter 2 -- DeviceTestContext pytest suite for MagnetPowerSupply.

Mocks the hardware, not the ramp logic itself -- the point is testing
the real GreenMode.Asyncio ramp physics (state transitions, settling,
Init during a ramp) against a deterministic fake "instrument" rather
than real serial I/O.

Real issue found building this: DeviceTestContext(process=False) --
running the device server in the SAME process as the test -- segfaults
under this device specifically, when the test polls state()/current in
a tight loop while the device's own asyncio ramp task is concurrently
running. Fixed by using process=True (a real subprocess), which is
also closer to how a real device server actually runs in production.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
import serial
from tango import AttrQuality, DevFailed, DevState
from tango.test_context import DeviceTestContext

from beamline_tango.devices import magnet_power_supply


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
            DeviceTestContext(
                magnet_power_supply.MagnetPowerSupply,
                # fast ramp -- tests shouldn't wait on real-world timing
                properties={"ramp_rate": 50.0},
                process=True,  # required for GreenMode.Asyncio -- see module docstring
            ) as proxy,
        ):
            yield proxy


@pytest.fixture
def fault_device():
    """A device that fails at startup: serial.Serial() raises, exercising
    the catch-and-set-FAULT path in init_device and, by extension, the
    is_setpoint_allowed/is_Reset_allowed FAULT-state guards."""
    with patch.object(magnet_power_supply, "serial") as mock_serial_module:
        mock_serial_module.SerialException = serial.SerialException
        mock_serial_module.Serial.side_effect = serial.SerialException(
            "could not open port"
        )
        with (
            DeviceTestContext(
                magnet_power_supply.MagnetPowerSupply,
                properties={"ramp_rate": 50.0},
                process=True,
            ) as proxy,
        ):
            deadline = time.time() + 3
            while time.time() < deadline and proxy.state() != DevState.FAULT:
                time.sleep(0.02)
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


def test_current_value_range_is_set(device):
    # Added for Ch8's Taurus panel work: a numeric widget's fill bar needs
    # a real min_value/max_value to render meaningfully -- confirmed via
    # a real "taurus form" against this device that the bar was always
    # solid full with no range defined, before this fix.
    config = device.get_attribute_config("current")
    assert float(config.min_value) == -1.0
    assert float(config.max_value) == 100.0


def test_setpoint_limits_are_set(device):
    # Ch32: the limits are on the device, so every client is bounded, not
    # only the Taurus GUI.
    config = device.get_attribute_config("setpoint")
    assert float(config.min_value) == 0.0
    assert float(config.max_value) == 100.0


@pytest.mark.parametrize("value", [-5.0, 150.0])
def test_setpoint_outside_limits_is_rejected(device, value):
    with pytest.raises(DevFailed) as excinfo:
        device.setpoint = value
    assert excinfo.value.args[0].reason == "API_WAttrOutsideLimit"
    assert device.state() == DevState.ON
    assert device.setpoint == pytest.approx(0.0)


def test_current_warning_bound_is_set(device):
    # Warning bounds exist only in the config's alarms struct (AttributeInfoEx),
    # unlike min_alarm/max_alarm, which AttributeInfo also has at the top level.
    alarms = device.get_attribute_config("current").alarms
    assert float(alarms.max_warning) == 90.0
    assert alarms.min_warning == "Not specified"


def test_current_above_warning_bound_reads_as_warning(device):
    device.setpoint = 95.0  # ~1.9 s ramp at the fixture's 50 A/s
    deadline = time.time() + 4
    while time.time() < deadline and device.state() != DevState.ON:
        time.sleep(0.05)
    reading = device.read_attribute("current")
    assert reading.value == pytest.approx(95.0, abs=1e-3)
    assert reading.quality == AttrQuality.ATTR_WARNING


def test_attributes_are_described(device):
    for name in ("setpoint", "current"):
        assert device.get_attribute_config(name).description != "No description"


def test_init_fault_on_serial_connection_failure(fault_device):
    assert fault_device.state() == DevState.FAULT
    assert "Failed to open serial port" in fault_device.status()


def test_setpoint_write_rejected_when_fault(fault_device):
    with pytest.raises(DevFailed):
        fault_device.setpoint = 5.0


def test_reset_rejected_when_fault(fault_device):
    with pytest.raises(DevFailed):
        fault_device.Reset()


def test_init_fault_on_invalid_ramp_rate():
    with patch.object(magnet_power_supply, "serial") as mock_serial_module:
        mock_serial_module.Serial.return_value = MagicMock()
        with (
            DeviceTestContext(
                magnet_power_supply.MagnetPowerSupply,
                properties={"ramp_rate": -1.0},
                process=True,
            ) as proxy,
        ):
            deadline = time.time() + 3
            while time.time() < deadline and proxy.state() != DevState.FAULT:
                time.sleep(0.02)
            assert proxy.state() == DevState.FAULT
            assert "ramp_rate must be positive" in proxy.status()


def _wait_settled(device, target, timeout=4.0):
    """Wait until the device is ON with its current at target (or time out)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if device.state() == DevState.ON and device.current == pytest.approx(
            target, abs=1e-3
        ):
            return
        time.sleep(0.02)


def test_init_during_ramp_settles_at_the_memorized_setpoint(device):
    # Init runs delete_device() then init_device(); Tango then restores the
    # hw_memorized setpoint (50 A here), which starts a new ramp from wherever
    # the current had got to. Until Ch32 this test also had Reset start a ramp
    # towards 0 A that Init had to stop. Reset now memorizes 0 A itself, so the
    # restored target and any leftover ramp agree, and that case can no longer
    # be built through Init. delete_device() still cancels the ramp, so that a
    # leftover task can't write to the closed serial port; that isn't tested.
    device.setpoint = 50.0  # 1 s ramp at the fixture's 50 A/s
    time.sleep(0.3)
    device.Init()
    _wait_settled(device, 50.0)
    assert device.state() == DevState.ON
    assert device.current == pytest.approx(50.0, abs=1e-3)


def _wait_ramp_over(device, target, timeout=4.0):
    """Wait until a ramp to target is over: current at target and State no
    longer MOVING, whatever State it ends in (or time out)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (
            device.current == pytest.approx(target, abs=1e-3)
            and device.state() != DevState.MOVING
        ):
            return
        time.sleep(0.02)


def test_operator_can_act_above_the_warning_level(device):
    # Ch32 (31 Oct): past current's max_warning (90 A) a client's State read
    # returns ALARM, and Tango keeps the device in ALARM. The setpoint write and
    # Reset accepted only ON/MOVING, so the operator could neither lower the
    # current nor Reset it: a lockout that came with the warning level itself
    # (5b9b9c1). Both are now allowed in ALARM.
    device.setpoint = 95.0  # ~1.9 s ramp at the fixture's 50 A/s
    _wait_ramp_over(device, 95.0)
    assert device.state() == DevState.ALARM
    device.setpoint = 50.0
    _wait_settled(device, 50.0)
    assert device.current == pytest.approx(50.0, abs=1e-3)

    device.setpoint = 95.0
    _wait_ramp_over(device, 95.0)
    assert device.state() == DevState.ALARM
    device.Reset()
    _wait_settled(device, 0.0)
    assert device.current == pytest.approx(0.0, abs=1e-3)


def test_reset_is_memorized(device):
    # Ch32 (30 Oct): Reset ramped to 0 A but left the memorized setpoint at the
    # last value a client had written, so the next restart (Init here) ramped
    # the magnet back up to it. Reset must memorize 0 A, as a client write
    # would.
    device.setpoint = 50.0
    _wait_settled(device, 50.0)
    device.Reset()
    _wait_settled(device, 0.0)
    assert device.read_attribute("setpoint").w_value == pytest.approx(0.0)
    device.Init()
    time.sleep(1.5)  # a 50 A ramp would be finished by now
    assert device.state() == DevState.ON
    assert device.setpoint == pytest.approx(0.0)
    assert device.current == pytest.approx(0.0, abs=1e-3)


def test_status_follows_state_after_recovery():
    # The first serial open fails (FAULT, with a failure status); a later Init
    # succeeds. The failure text must not outlive the failure.
    hw = FakeHardware(initial=0.0)

    async def fake_query_current(self):  # noqa: ARG001
        return hw.current

    with patch.object(magnet_power_supply, "serial") as mock_serial_module:
        mock_serial_module.SerialException = serial.SerialException
        mock_serial_module.Serial.side_effect = [
            serial.SerialException("could not open port"),
            MagicMock(),
        ]
        with (
            patch.object(
                magnet_power_supply.MagnetPowerSupply,
                "_query_current",
                fake_query_current,
            ),
            DeviceTestContext(
                magnet_power_supply.MagnetPowerSupply,
                properties={"ramp_rate": 50.0},
                process=True,
            ) as proxy,
        ):
            assert proxy.state() == DevState.FAULT
            assert "Failed to open serial port" in proxy.status()
            proxy.Init()
            assert proxy.state() == DevState.ON
            assert "Failed" not in proxy.status()

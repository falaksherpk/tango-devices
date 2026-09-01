import time
from unittest.mock import patch

import pytest
from tango import DevState
from tango.test_context import DeviceTestContext

from temperature_sensor import TemperatureSensor


@pytest.fixture
def device():
    # Patch random.uniform so the background drift thread adds exactly 0.0
    # every tick -- makes the tests deterministic instead of racing against
    # a genuinely random background thread. DeviceTestContext spins the
    # device up in-process, with no real Tango Database required.
    with (
        patch("temperature_sensor.random.uniform", return_value=0.0),
        DeviceTestContext(TemperatureSensor, process=False) as proxy,
    ):
        yield proxy


def test_initial_state_is_on(device):
    assert device.state() == DevState.ON


def test_initial_temperature_is_baseline(device):
    assert device.Temperature == pytest.approx(20.0)


def test_temperature_alarm_bounds_are_set(device):
    config = device.get_attribute_config("Temperature")
    assert float(config.min_alarm) == 19.0
    assert float(config.max_alarm) == 21.0


def test_reset_returns_to_baseline(device):
    device.Reset()
    assert device.Temperature == pytest.approx(20.0)


def test_pressure_is_derived_from_temperature(device):
    # At the 20.0 baseline: Pressure = 1.0 + (20.0 - 20.0) * 0.05 = 1.0
    assert device.Pressure == pytest.approx(1.0)


@pytest.fixture
def device_forced_low():
    # First drift tick forces Temperature well below the alarm threshold;
    # every tick after that adds 0 so it settles rather than drifting further.
    with (
        patch("temperature_sensor.random.uniform", side_effect=[-5.0] + [0.0] * 1000),
        DeviceTestContext(TemperatureSensor, process=False) as proxy,
    ):
        yield proxy


def test_alarm_triggers_when_temperature_drifts_low(device_forced_low):
    # Poll with a short timeout rather than a fixed sleep -- more robust
    # against timing variance than guessing exactly how long the
    # background thread needs to apply its first tick.
    deadline = time.time() + 3
    while time.time() < deadline:
        if device_forced_low.state() == DevState.ALARM:
            break
        time.sleep(0.1)
    assert device_forced_low.state() == DevState.ALARM
    assert device_forced_low.Temperature < 19.0

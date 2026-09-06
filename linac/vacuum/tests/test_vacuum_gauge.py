"""
Part 3 Chapter 5 -- DeviceTestContext pytest suite for VacuumGauge.

Architectural note, and why this suite's approach differs from Ch2/
Ch3/Ch4: this device is a plain synchronous Device (no
GreenMode.Asyncio) using a background threading.Thread poller and a
blocking pymodbus.ModbusTcpClient -- closer in shape to Phase 1's
TemperatureSensor than to any device tested so far in this book.
There is no established precedent in this project for testing this
shape via DeviceTestContext, so process=False (in-process, the
simpler/faster default) is tried first empirically rather than
copying Ch2/Ch3/Ch4's process=True, since their reason for it
(GreenMode.Asyncio segfaulting under concurrent polling) does not
apply to a plain synchronous device.

Like Ch4, real fake-hardware subprocesses on dynamic loopback ports
are used per test function rather than mocking pymodbus's
ModbusTcpClient directly -- it's a real third-party network client,
not project code, so mocking its internals would be brittle.

redis.Redis is mocked with a plain MagicMock (not AsyncMock, unlike
Ch4) -- VacuumGauge's Redis usage is genuinely synchronous
(redis.Redis(...).xadd(...) is a normal blocking call), so AsyncMock
would be the wrong tool here.
"""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tango
from tango import DevState
from tango.test_context import DeviceTestContext

import vacuum_gauge

VACUUM_DIR = Path(__file__).resolve().parent.parent
SIMULATOR_SCRIPT = VACUUM_DIR / "fake_modbus_vacuum.py"


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} not accepting connections after {timeout}s")


def stop_proc(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture
def fake_hardware():
    port = get_free_port()
    proc = subprocess.Popen([sys.executable, str(SIMULATOR_SCRIPT), str(port)])
    wait_for_port(port)
    state = {"port": port, "proc": proc}
    yield state
    stop_proc(state["proc"])


@pytest.fixture
def device(fake_hardware):
    with (
        patch.object(vacuum_gauge.redis, "Redis", return_value=MagicMock()),
        DeviceTestContext(
            vacuum_gauge.VacuumGauge,
            properties={
                "Host": "localhost",
                "Port": fake_hardware["port"],
                "ModbusDeviceId": 1,
            },
            process=False,  # empirical: not GreenMode.Asyncio, see module docstring
        ) as proxy,
    ):
        yield proxy


def test_initial_state_is_on(device):
    assert device.state() == DevState.ON


def test_initial_pump_status_and_pressure(device):
    assert device.PumpStatus is False
    assert device.VacuumPressure == pytest.approx(1.0, abs=0.001)


def test_pump_on_off_via_attribute_and_commands(device):
    device.PumpStatus = True
    assert device.PumpStatus is True

    result = device.command_inout("PumpOff")
    assert "OFF" in result
    assert device.PumpStatus is False

    result = device.command_inout("PumpOn")
    assert "ON" in result
    assert device.PumpStatus is True


def test_pressure_decays_from_actual_current_state(device):
    """Regression test for the single most important historical bug
    this device carries forward (Phase 2 Lab 2.3): pressure must decay
    from wherever it actually currently is, not from a hardcoded
    baseline -- otherwise turning the pump on a second time after
    settling at a lower value makes pressure jump back UP, which is
    physically backwards for a vacuum pump."""
    device.PumpStatus = True
    time.sleep(2)
    first_stop_pressure = device.VacuumPressure
    device.PumpStatus = False
    time.sleep(0.5)
    assert device.VacuumPressure == pytest.approx(first_stop_pressure, abs=0.01)

    device.PumpStatus = True
    time.sleep(2)
    second_reading = device.VacuumPressure
    device.PumpStatus = False

    assert second_reading < first_stop_pressure, (
        f"pressure ({second_reading}) should have continued decaying below "
        f"where it stopped ({first_stop_pressure}), not reset upward"
    )


def test_change_event_fires_on_pressure_change(device):
    events = []
    eid = device.subscribe_event(
        "VacuumPressure", tango.EventType.CHANGE_EVENT, lambda evt: events.append(evt), stateless=True
    )
    try:
        device.PumpStatus = True
        time.sleep(2.5)
        device.PumpStatus = False
    finally:
        device.unsubscribe_event(eid)

    assert len(events) >= 2, "expected at least the initial event plus one real change"


def test_concurrent_pump_status_writes_are_serialized(device):
    """Scaled-down regression test for the real live concurrency proof
    done manually in LAB 5.4 (100 concurrent writes, 0 errors, 0
    dropped/duplicated Modbus transactions) -- confirms the carried-
    forward threading.Lock design holds under automated, repeatable
    concurrent load too."""
    errors = []

    def hammer(n):
        for i in range(5):
            try:
                device.PumpStatus = bool((i + n) % 2)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert device.state() == DevState.ON

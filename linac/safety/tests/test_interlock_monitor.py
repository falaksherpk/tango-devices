"""
Part 3 Chapter 6 -- DeviceTestContext pytest suite for InterlockMonitor.

Same shape as Ch5's VacuumGauge suite: this device is a plain
synchronous Device (no GreenMode.Asyncio) using a background
threading.Thread poller and a blocking pymodbus client, so
process=False and real fake-hardware subprocesses on dynamic ports
are used, same reasoning as Ch5 -- pymodbus's client is real
third-party network code, not mockable _blocking_* methods.

FAULT-path tests are included from the start here, unlike Ch5 where
they were added at the compliance-audit stage -- a deliberate change
this chapter, matching interlock_monitor.py's own build-compliance-in
approach.
"""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from tango import DevFailed, DevState
from tango.test_context import DeviceTestContext

import interlock_monitor

SAFETY_DIR = Path(__file__).resolve().parent.parent
SIMULATOR_SCRIPT = SAFETY_DIR / "fake_modbus_plc.py"

ADDR_DOOR, ADDR_VACUUM, ADDR_WATER, ADDR_PERMIT, ADDR_FAULT, ADDR_RESET = range(6)


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
    with DeviceTestContext(
        interlock_monitor.InterlockMonitor,
        properties={"Host": "localhost", "Port": fake_hardware["port"]},
        process=False,
    ) as proxy:
        yield proxy


@pytest.fixture
def device_factory():
    """Yields a callable that starts a device with arbitrary properties,
    for deliberately-broken configurations. No fake hardware started."""
    contexts = []

    def _make(**properties):
        ctx = DeviceTestContext(
            interlock_monitor.InterlockMonitor, properties=properties, process=False
        )
        contexts.append(ctx)
        return ctx.__enter__()

    yield _make
    for ctx in reversed(contexts):
        ctx.__exit__(None, None, None)


def _raw_plc_client(fake_hardware):
    from pymodbus.client import ModbusTcpClient

    c = ModbusTcpClient("127.0.0.1", port=fake_hardware["port"])
    c.connect()
    return c


def test_initial_state_is_on(device):
    assert device.state() == DevState.ON


def test_initial_attributes(device):
    assert device.BeamPermit is True
    assert device.FaultLatched is False
    assert device.DoorClosed is True
    assert device.VacuumOk is True
    assert device.WaterFlowOk is True


def test_door_opens_triggers_alarm_and_latches_fault(device, fake_hardware):
    plc = _raw_plc_client(fake_hardware)
    plc.write_coil(address=ADDR_DOOR, value=False, device_id=1)
    time.sleep(1.5)

    assert device.state() == DevState.ALARM
    assert device.BeamPermit is False
    assert device.FaultLatched is True
    plc.close()


def test_input_recovery_alone_does_not_clear_fault(device, fake_hardware):
    """Regression test for the design's core safety behaviour: a
    recovered input must NOT silently restore permit."""
    plc = _raw_plc_client(fake_hardware)
    plc.write_coil(address=ADDR_DOOR, value=False, device_id=1)
    time.sleep(1.5)
    plc.write_coil(address=ADDR_DOOR, value=True, device_id=1)
    time.sleep(1.5)

    assert device.state() == DevState.ALARM
    assert device.FaultLatched is True, "recovering the input must not clear the latch"
    assert device.BeamPermit is False
    plc.close()


def test_reset_refused_while_cause_persists(device, fake_hardware):
    plc = _raw_plc_client(fake_hardware)
    plc.write_coil(address=ADDR_VACUUM, value=False, device_id=1)
    time.sleep(1.5)

    device.Reset()
    time.sleep(1.5)

    assert device.FaultLatched is True, (
        "reset must not clear a fault while its cause persists"
    )
    assert device.state() == DevState.ALARM
    plc.close()


def test_reset_clears_fault_once_inputs_safe(device, fake_hardware):
    plc = _raw_plc_client(fake_hardware)
    plc.write_coil(address=ADDR_WATER, value=False, device_id=1)
    time.sleep(1.5)
    plc.write_coil(address=ADDR_WATER, value=True, device_id=1)
    time.sleep(1.5)
    assert device.FaultLatched is True

    device.Reset()
    time.sleep(1.5)

    assert device.FaultLatched is False
    assert device.BeamPermit is True
    assert device.state() == DevState.ON
    plc.close()


def test_concurrent_reset_calls_are_serialized(device):
    """Scaled-down regression test for the real live concurrency proof
    (100 concurrent Reset() calls, 0 errors, exactly 100 writes
    confirmed reaching the instrument)."""
    errors = []

    def hammer():
        for _ in range(5):
            try:
                device.Reset()
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))

    threads = [threading.Thread(target=hammer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert device.state() == DevState.ON


@pytest.mark.parametrize(
    "bad_properties, expected_in_status",
    [
        ({"Port": 0}, "Port must be"),
        ({"Port": 70000}, "Port must be"),
    ],
)
def test_invalid_properties_fault_at_startup(
    device_factory, bad_properties, expected_in_status
):
    properties = {"Host": "127.0.0.1", "Port": 5040}
    properties.update(bad_properties)
    proxy = device_factory(**properties)

    assert proxy.state() == DevState.FAULT
    assert expected_in_status in proxy.status()


def test_unreachable_host_faults_at_startup(device_factory):
    proxy = device_factory(Host="127.0.0.1", Port=get_free_port())

    assert proxy.state() == DevState.FAULT
    assert "Failed to connect" in proxy.status()


def test_faulted_device_refuses_reads_and_commands(device_factory):
    proxy = device_factory(Host="127.0.0.1", Port=get_free_port())
    assert proxy.state() == DevState.FAULT

    for attr in ("BeamPermit", "FaultLatched", "DoorClosed", "VacuumOk", "WaterFlowOk"):
        with pytest.raises(DevFailed):
            _ = getattr(proxy, attr)

    with pytest.raises(DevFailed):
        proxy.command_inout("Reset")


def test_faults_on_instrument_loss_then_recovers(device, fake_hardware):
    assert device.state() == DevState.ON

    stop_proc(fake_hardware["proc"])

    deadline = time.time() + 15
    while time.time() < deadline and device.state() != DevState.FAULT:
        time.sleep(0.5)
    assert device.state() == DevState.FAULT, "device stayed non-FAULT while blind"

    with pytest.raises(DevFailed):
        _ = device.BeamPermit

    fake_hardware["proc"] = subprocess.Popen(
        [sys.executable, str(SIMULATOR_SCRIPT), str(fake_hardware["port"])]
    )
    wait_for_port(fake_hardware["port"])

    deadline = time.time() + 15
    while time.time() < deadline and device.state() != DevState.ON:
        time.sleep(0.5)
    assert device.state() == DevState.ON, "device did not recover on its own"

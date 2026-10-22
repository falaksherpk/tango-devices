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

Ch28 adds the lifetime tests (as Ch27 did for the vacuum gauge): a
device that faulted at startup tears down cleanly, Init closes the old
Modbus connection and leaves exactly one poll loop, a device started or
Init-ed while the PLC is down recovers by itself once it comes back, and
a hung PLC can't block Init. Also: Reset reports a refused or
undelivered write, and recovery is logged at INFO, not ERROR.
"""
import contextlib
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException
from pymodbus.pdu import ExceptionResponse
from tango import DevFailed, DevState
from tango.test_context import DeviceTestContext

import interlock_monitor

SAFETY_DIR = Path(__file__).resolve().parent.parent
SIMULATOR_SCRIPT = SAFETY_DIR / "fake_modbus_plc.py"
POLL_THREAD_NAME = "InterlockMonitor-poll"

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


def start_simulator(port: int) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(SIMULATOR_SCRIPT), str(port)])
    wait_for_port(port)
    return proc


def poll_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == POLL_THREAD_NAME)


def wait_for_state(proxy, state, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and proxy.state() != state:
        time.sleep(0.2)
    assert proxy.state() == state, f"still {proxy.state()} after {timeout}s"


@pytest.fixture
def fake_hardware():
    port = get_free_port()
    state = {"port": port, "proc": start_simulator(port)}
    yield state
    if state["proc"] is not None:
        stop_proc(state["proc"])


@pytest.fixture
def device(fake_hardware):
    with DeviceTestContext(
        interlock_monitor.InterlockMonitor,
        properties={
            "Host": "127.0.0.1",  # the fake binds IPv4 only
            "Port": fake_hardware["port"],
        },
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

    fake_hardware["proc"] = start_simulator(fake_hardware["port"])

    deadline = time.time() + 15
    while time.time() < deadline and device.state() != DevState.ON:
        time.sleep(0.5)
    assert device.state() == DevState.ON, "device did not recover on its own"


def test_recovers_to_alarm_not_on_if_still_tripped(device, fake_hardware):
    """Untested-until-now path (Ch6 audit Finding B): recovering from
    FAULT must land in the state that actually matches reality. If the
    interlock was genuinely tripped at the moment comms return, the
    device must report ALARM, not silently default to ON."""
    plc = _raw_plc_client(fake_hardware)
    plc.write_coil(address=ADDR_DOOR, value=False, device_id=1)
    time.sleep(1.5)
    assert device.state() == DevState.ALARM
    plc.close()

    stop_proc(fake_hardware["proc"])
    deadline = time.time() + 15
    while time.time() < deadline and device.state() != DevState.FAULT:
        time.sleep(0.5)
    assert device.state() == DevState.FAULT

    # Bring the instrument back WITHOUT clearing the door -- the
    # interlock is still genuinely tripped when comms return.
    fake_hardware["proc"] = start_simulator(fake_hardware["port"])
    new_plc = _raw_plc_client(fake_hardware)
    new_plc.write_coil(address=ADDR_DOOR, value=False, device_id=1)

    deadline = time.time() + 15
    while time.time() < deadline and device.state() == DevState.FAULT:
        time.sleep(0.5)

    assert device.state() == DevState.ALARM, (
        "device recovered from FAULT but reported the wrong state -- "
        "the interlock was still genuinely tripped"
    )
    assert device.BeamPermit is False
    new_plc.close()


# --- Lifetime across Init and startup (Ch28) -------------------------


def test_delete_device_after_startup_fault_does_not_raise(device_factory, capfd):
    """A device that faulted at startup must tear down cleanly: Init calls
    delete_device() first, and so does server shutdown."""
    proxy = device_factory(Host="127.0.0.1", Port=0)
    assert proxy.state() == DevState.FAULT
    proxy.Init()
    proxy.Init()
    assert "delete_device() raised" not in capfd.readouterr().err


def test_init_closes_the_old_connection(device):
    """Init must close the previous Modbus connection, not leak one per
    call. Every client the device creates is recorded and kept referenced,
    so a leaked one cannot be closed behind the device's back by the
    garbage collector (Ch27)."""
    clients = []

    class RecordingClient(ModbusTcpClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    with patch.object(interlock_monitor, "ModbusTcpClient", RecordingClient):
        for _ in range(5):
            device.Init()
        time.sleep(1.5)
    assert device.state() == DevState.ON
    assert len(clients) == 5
    assert [c.connected for c in clients] == [False] * 4 + [True]


def test_init_leaves_exactly_one_poll_loop(device):
    for _ in range(3):
        device.Init()
    time.sleep(1.5)
    assert device.state() == DevState.ON
    assert poll_threads() == 1


def test_starts_in_fault_then_recovers_when_plc_appears(device_factory):
    """Started before its PLC, the device faults with a clear status, then
    comes to ON by itself once the PLC answers."""
    port = get_free_port()
    proxy = device_factory(Host="127.0.0.1", Port=port)
    assert proxy.state() == DevState.FAULT
    assert "Failed to connect" in proxy.status()

    proc = start_simulator(port)
    try:
        wait_for_state(proxy, DevState.ON)
        assert proxy.BeamPermit is True
        assert "Failed" not in proxy.status()
    finally:
        stop_proc(proc)


@pytest.fixture
def hung_plc():
    """A peer that accepts connections and never answers: a hung PLC."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    held, stop = [], threading.Event()

    def accept_loop():
        while not stop.is_set():
            with contextlib.suppress(OSError):  # the 0.2 s accept timeout
                held.append(srv.accept()[0])

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(timeout=2)
    for conn in held:
        conn.close()
    srv.close()


def test_hung_plc_faults_and_init_stays_bounded(device_factory, hung_plc):
    """A PLC that accepts and never answers: the device faults, and Init
    returns well inside the 3 s client timeout and leaves exactly one poll
    loop (a poll blocked in a long read must not outlive its device)."""
    proxy = device_factory(Host="127.0.0.1", Port=hung_plc)
    assert proxy.state() == DevState.FAULT
    for _ in range(2):
        t0 = time.time()
        proxy.Init()
        assert time.time() - t0 < 2.9
    time.sleep(1.5)
    assert poll_threads() == 1


def test_recovers_after_a_hung_plc_returns(device, fake_hardware):
    """A PLC that hangs (accepts, never answers) and then comes back: the
    device must reconnect and return to ON by itself. Found live in Ch28:
    after a timed-out read, pymodbus keeps its socket; when the peer later
    closes it, every poll failed with EPIPE on the dead socket and the
    device stayed FAULT until Init."""
    port = fake_hardware["port"]
    stop_proc(fake_hardware["proc"])
    fake_hardware["proc"] = None

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    srv.settimeout(0.2)
    held, stop = [], threading.Event()

    def accept_loop():
        while not stop.is_set():
            with contextlib.suppress(OSError):  # the 0.2 s accept timeout
                held.append(srv.accept()[0])

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        wait_for_state(device, DevState.FAULT)
        time.sleep(1.5)  # at least one more timed-out read on the hung link
        assert held, "the device never connected to the hung PLC"
    finally:
        stop.set()
        t.join(timeout=2)
        for conn in held:
            conn.close()
        srv.close()

    fake_hardware["proc"] = start_simulator(port)
    wait_for_state(device, DevState.ON, timeout=10)
    assert device.BeamPermit is True


def test_init_during_outage_then_recovers(device, fake_hardware):
    """Init while the PLC is down leaves a device that still recovers by
    itself."""
    stop_proc(fake_hardware["proc"])
    fake_hardware["proc"] = None
    wait_for_state(device, DevState.FAULT)

    device.Init()
    assert device.state() == DevState.FAULT

    fake_hardware["proc"] = start_simulator(fake_hardware["port"])
    wait_for_state(device, DevState.ON)
    assert device.BeamPermit is True
    assert poll_threads() == 1


def test_recovery_is_logged_at_info_not_error(device, fake_hardware):
    """Contact restored is good news: INFO. ERROR is for the failures."""
    infos, errors = [], []

    def record(store):
        return lambda _self, msg, *_args: store.append(msg)

    with (
        patch.object(
            interlock_monitor.InterlockMonitor, "info_stream", record(infos)
        ),
        patch.object(
            interlock_monitor.InterlockMonitor, "error_stream", record(errors)
        ),
    ):
        stop_proc(fake_hardware["proc"])
        fake_hardware["proc"] = None
        wait_for_state(device, DevState.FAULT)
        fake_hardware["proc"] = start_simulator(fake_hardware["port"])
        wait_for_state(device, DevState.ON)

    assert any("Poll failed" in m for m in errors)
    assert any("restored" in m for m in infos)
    assert not any("restored" in m for m in errors)


# --- Reset reports what happened to its write (Ch28) -----------------


def _client_with_write_coil(behaviour):
    class Client(ModbusTcpClient):
        def write_coil(self, *_args, **_kwargs):
            return behaviour()

    return Client


def _refused():
    return ExceptionResponse(5, 2)  # function code 5, ILLEGAL DATA ADDRESS


def _not_delivered():
    raise ConnectionException("simulated: connection lost during the write")


@pytest.mark.parametrize(
    "behaviour, reason",
    [
        (_refused, "InterlockMonitor_ResetRefused"),
        (_not_delivered, "InterlockMonitor_ResetNotDelivered"),
    ],
)
def test_reset_reports_a_failed_write(fake_hardware, device_factory, behaviour, reason):
    with patch.object(
        interlock_monitor, "ModbusTcpClient", _client_with_write_coil(behaviour)
    ):
        proxy = device_factory(Host="127.0.0.1", Port=fake_hardware["port"])
    assert proxy.state() == DevState.ON
    with pytest.raises(DevFailed) as excinfo:
        proxy.Reset()
    assert excinfo.value.args[0].reason == reason

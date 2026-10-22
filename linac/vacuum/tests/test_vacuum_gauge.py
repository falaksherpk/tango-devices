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

Ch27 adds the lifetime tests: a device that faulted at startup tears
down cleanly, Init closes the old Modbus connection and leaves exactly
one poll loop, and a device started or Init-ed while the instrument is
down recovers by itself once it comes back.
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
import tango
from tango import DevFailed, DevState
from pymodbus.client import ModbusTcpClient
from tango.test_context import DeviceTestContext

import vacuum_gauge

VACUUM_DIR = Path(__file__).resolve().parent.parent
SIMULATOR_SCRIPT = VACUUM_DIR / "fake_modbus_vacuum.py"
POLL_THREAD_NAME = "VacuumGauge-poll"


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
        vacuum_gauge.VacuumGauge,
        properties={
            "Host": "127.0.0.1",  # the fake binds IPv4 only
            "Port": fake_hardware["port"],
            "ModbusDeviceId": 1,
        },
        process=False,  # empirical: not GreenMode.Asyncio, see module docstring
    ) as proxy:
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
    # VacuumPressure is served from a cache the poll loop refreshes once a
    # second, so a read taken shortly after a write can still reflect the
    # pre-write state. Every reading below is therefore taken only after the
    # cache has had more than a full poll cycle to settle -- an earlier
    # version of this test read 0.5s after PumpStatus=False and passed
    # locally but failed in CI on exactly that race.
    POLL_SETTLE = 2.5

    device.PumpStatus = True
    time.sleep(2)
    device.PumpStatus = False
    time.sleep(POLL_SETTLE)
    first_stop_pressure = device.VacuumPressure

    # With the pump off, two settled readings must agree: pressure holds.
    time.sleep(POLL_SETTLE)
    assert device.VacuumPressure == pytest.approx(first_stop_pressure, abs=0.001)

    device.PumpStatus = True
    time.sleep(2)
    device.PumpStatus = False
    time.sleep(POLL_SETTLE)
    second_reading = device.VacuumPressure

    assert second_reading < first_stop_pressure, (
        f"pressure ({second_reading}) should have continued decaying below "
        f"where it stopped ({first_stop_pressure}), not reset upward"
    )


def test_change_event_fires_on_pressure_change(device):
    events = []
    eid = device.subscribe_event(
        "VacuumPressure",
        tango.EventType.CHANGE_EVENT,
        lambda evt: events.append(evt),
        sub_mode=tango.EventSubMode.Stateless,
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


# --- FAULT-path coverage (Ch5 LAB 5.6, Finding E) -------------------
# Ch2's own compliance audit flagged zero FAULT-path test coverage as a
# real gap. These close it for this device, and lock in the fault
# behaviour added in LAB 5.6 after live testing showed the
# carried-forward design stayed ON while blind.


@pytest.fixture
def device_factory():
    """Yields a callable that starts a device with arbitrary properties,
    so a test can build deliberately-broken configurations. Unlike the
    `device` fixture, no fake hardware is started."""
    contexts = []

    def _make(**properties):
        ctx = DeviceTestContext(
            vacuum_gauge.VacuumGauge, properties=properties, process=False
        )
        contexts.append(ctx)
        return ctx.__enter__()

    yield _make
    for ctx in reversed(contexts):
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "bad_properties, expected_in_status",
    [
        ({"Port": 0}, "Port must be"),
        ({"Port": 70000}, "Port must be"),
        ({"ModbusDeviceId": -1}, "ModbusDeviceId must be"),
        ({"ModbusDeviceId": 248}, "ModbusDeviceId must be"),
    ],
)
def test_invalid_properties_fault_at_startup(
    device_factory, bad_properties, expected_in_status
):
    """Startup validation rejects out-of-range properties with a clear
    status, rather than failing obscurely later at connect time."""
    properties = {"Host": "127.0.0.1", "Port": 5020, "ModbusDeviceId": 1}
    properties.update(bad_properties)
    proxy = device_factory(**properties)

    assert proxy.state() == DevState.FAULT
    assert expected_in_status in proxy.status()


def test_unreachable_host_faults_at_startup(device_factory):
    """A valid-but-unreachable endpoint faults cleanly at startup."""
    proxy = device_factory(
        Host="127.0.0.1", Port=get_free_port(), ModbusDeviceId=1
    )

    assert proxy.state() == DevState.FAULT
    assert "Failed to connect" in proxy.status()


def test_faulted_device_refuses_reads_and_commands(device_factory):
    """While faulted, the device must not serve a stale cached pressure
    or accept pump commands -- the core safety fix from LAB 5.6."""
    proxy = device_factory(
        Host="127.0.0.1", Port=get_free_port(), ModbusDeviceId=1
    )
    assert proxy.state() == DevState.FAULT

    for attr in ("VacuumPressure", "PumpStatus"):
        with pytest.raises(DevFailed):
            getattr(proxy, attr)

    for cmd in ("PumpOn", "PumpOff"):
        with pytest.raises(DevFailed):
            proxy.command_inout(cmd)


def test_faults_on_instrument_loss_then_recovers(device, fake_hardware):
    """Regression test for the real behaviour found by live testing in
    LAB 5.6: killing the instrument mid-run must drive the device to
    FAULT (not leave it ON serving stale data), and it must return to
    ON by itself once the instrument comes back -- pymodbus reconnects
    internally, so no hand-written reconnect logic is involved."""
    assert device.state() == DevState.ON

    stop_proc(fake_hardware["proc"])

    deadline = time.time() + 15
    while time.time() < deadline and device.state() != DevState.FAULT:
        time.sleep(0.5)
    assert device.state() == DevState.FAULT, "device stayed ON while blind"
    assert "Lost contact" in device.status()

    with pytest.raises(DevFailed):
        _ = device.VacuumPressure

    # Bring the same port back up and confirm unassisted recovery.
    fake_hardware["proc"] = start_simulator(fake_hardware["port"])

    deadline = time.time() + 15
    while time.time() < deadline and device.state() != DevState.ON:
        time.sleep(0.5)
    assert device.state() == DevState.ON, "device did not recover on its own"
    assert device.VacuumPressure > 0


# --- Lifetime across Init and startup (Ch27) -------------------------


def test_delete_device_after_startup_fault_does_not_raise(device_factory, capfd):
    """A device that faulted at startup must tear down cleanly: Init calls
    delete_device() first, and so does server shutdown."""
    proxy = device_factory(Host="127.0.0.1", Port=0, ModbusDeviceId=1)
    assert proxy.state() == DevState.FAULT
    proxy.Init()
    proxy.Init()
    assert "delete_device() raised" not in capfd.readouterr().err


def test_init_closes_the_old_connection(device):
    """Init must close the previous Modbus connection, not leak one per
    call. Every client the device creates is recorded and kept referenced,
    so a leaked one cannot be closed behind the device's back by the
    garbage collector (counting sockets from outside gave 1, 2 or 6 for
    the same leak, Ch27)."""
    clients = []

    class RecordingClient(ModbusTcpClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    with patch.object(vacuum_gauge, "ModbusTcpClient", RecordingClient):
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


def test_starts_in_fault_then_recovers_when_instrument_appears(device_factory):
    """Started before its instrument, the device faults with a clear
    status, then comes to ON by itself once the instrument answers."""
    port = get_free_port()
    proxy = device_factory(Host="127.0.0.1", Port=port, ModbusDeviceId=1)
    assert proxy.state() == DevState.FAULT
    assert "Failed to connect" in proxy.status()

    proc = start_simulator(port)
    try:
        wait_for_state(proxy, DevState.ON)
        assert proxy.VacuumPressure == pytest.approx(1.0, abs=0.001)
        assert "Failed" not in proxy.status()
    finally:
        stop_proc(proc)


@pytest.fixture
def hung_instrument():
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


def test_hung_instrument_faults_and_init_stays_bounded(
    device_factory, hung_instrument
):
    """An instrument that accepts and never answers: the device faults,
    and Init returns well inside the 3 s client timeout and leaves exactly
    one poll loop (a poll blocked in a long read must not outlive its
    device)."""
    proxy = device_factory(Host="127.0.0.1", Port=hung_instrument, ModbusDeviceId=1)
    assert proxy.state() == DevState.FAULT
    for _ in range(2):
        t0 = time.time()
        proxy.Init()
        assert time.time() - t0 < 2.9
    time.sleep(1.5)
    assert poll_threads() == 1


def test_recovers_after_a_hung_instrument_returns(device, fake_hardware):
    """An instrument that hangs (accepts, never answers) and then comes back:
    the device must reconnect and return to ON by itself. Found live in Ch28
    on the interlock (same client pattern): after a timed-out read, pymodbus
    keeps its socket; when the peer later closes it, every poll failed with
    EPIPE on the dead socket and the device stayed FAULT until Init."""
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
        assert held, "the device never connected to the hung instrument"
    finally:
        stop.set()
        t.join(timeout=2)
        for conn in held:
            conn.close()
        srv.close()

    fake_hardware["proc"] = start_simulator(port)
    wait_for_state(device, DevState.ON, timeout=10)
    assert device.VacuumPressure > 0


def test_init_during_outage_then_recovers(device, fake_hardware):
    """Init while the instrument is down leaves a device that still
    recovers by itself."""
    stop_proc(fake_hardware["proc"])
    fake_hardware["proc"] = None
    wait_for_state(device, DevState.FAULT)

    device.Init()
    assert device.state() == DevState.FAULT

    fake_hardware["proc"] = start_simulator(fake_hardware["port"])
    wait_for_state(device, DevState.ON)
    assert device.VacuumPressure > 0
    assert poll_threads() == 1

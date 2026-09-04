"""
Part 3 Chapter 4 -- DeviceTestContext pytest suite for FaradayCup.

Architectural difference from Ch2/Ch3, and why this suite's approach
differs from theirs: this device uses native asyncio.open_connection
(StreamReader/StreamWriter) directly, with no "_blocking_*" instance
methods to patch.object() the way the magnet/camera do. Rather than
invent a parallel mock layer, this suite runs the already-proven real
fake hardware (fake_picoammeter.py + fake_prologix_adapter.py) as real
OS subprocesses on dynamically-allocated loopback ports, and points
DeviceTestContext at them via the device's real PrologixHost/
PrologixPort properties. This sidesteps Ch2/Ch3's fork-visibility
problem entirely (no multiprocessing.Value needed): a forked
DeviceTestContext(process=True) child connects to a real listening
TCP port, which needs no shared-memory trick to be visible across the
fork boundary.

Trade-off, stated honestly: this makes each test slower (subprocess
startup overhead) than Ch2/Ch3's in-process mocks. In exchange, a
fresh picoammeter+adapter pair is started per TEST FUNCTION (not per
session) specifically to avoid the real cross-test staleness bug found
live while building this chapter's manual test client (a stale,
unclaimed reply left in the adapter's persistent backend connection
was returned to a later, unrelated caller). Session-scoped fake
hardware would risk silently reintroducing exactly that failure mode
between tests.

process=True is required for the same reason established in Chapter
2's magnet suite and re-confirmed in Chapter 3: DeviceTestContext
(process=False) segfaults for GreenMode.Asyncio devices under
concurrent polling. This device shares that architecture.

redis.Redis is mocked to avoid a real Redis dependency in CI, matching
Ch2/Ch3's precedent -- this suite does not assert anything about what
the mock's .xadd() received, for the same reason Ch2/Ch3 don't:
process=True forks a separate OS process, so a mock's recorded calls
in that forked child are not observable from the test process.
"""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import tango
from tango import DevState
from tango.test_context import DeviceTestContext

import faraday_cup

BEAM_DIR = Path(__file__).resolve().parent.parent
PICOAMMETER_SCRIPT = BEAM_DIR / "fake_picoammeter.py"
ADAPTER_SCRIPT = BEAM_DIR / "fake_prologix_adapter.py"

BASELINE_CURRENT = 2.5e-9
BASELINE_TOLERANCE = 5e-10
ZERO_CHECK_TOLERANCE = 1e-11


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


def start_adapter(adapter_port: int, pico_port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(ADAPTER_SCRIPT),
         str(adapter_port), "localhost", str(pico_port)]
    )
    wait_for_port(adapter_port)
    return proc


def stop_proc(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture
def fake_hardware():
    pico_port = get_free_port()
    adapter_port = get_free_port()

    pico_proc = subprocess.Popen(
        [sys.executable, str(PICOAMMETER_SCRIPT), str(pico_port)]
    )
    wait_for_port(pico_port)

    adapter_proc = start_adapter(adapter_port, pico_port)

    state = {
        "pico_port": pico_port,
        "adapter_port": adapter_port,
        "pico_proc": pico_proc,
        "adapter_proc": adapter_proc,
    }
    yield state

    stop_proc(state["adapter_proc"])
    stop_proc(state["pico_proc"])


@pytest.fixture
def device(fake_hardware):
    with (
        patch.object(faraday_cup.redis, "Redis", return_value=AsyncMock()),
        DeviceTestContext(
            faraday_cup.FaradayCup,
            properties={
                "PrologixHost": "localhost",
                "PrologixPort": fake_hardware["adapter_port"],
                "GpibAddress": 22,
                "PollIntervalMs": 100,
            },
            process=True,  # required for GreenMode.Asyncio -- see module docstring
        ) as proxy,
    ):
        yield proxy


def test_initial_state_is_on(device):
    assert device.state() == DevState.ON


def test_current_reads_near_baseline(device):
    value = device.current
    assert abs(value - BASELINE_CURRENT) < BASELINE_TOLERANCE


def test_zero_check_changes_current_reading(device):
    """Direct pytest equivalent of what was confirmed manually earlier
    this chapter: zero_check is a real write that changes real
    instrument behavior, not just locally-mirrored state."""
    assert device.zero_check is False

    device.zero_check = True
    assert device.zero_check is True
    zeroed_value = device.current
    assert abs(zeroed_value) < ZERO_CHECK_TOLERANCE

    device.zero_check = False
    assert device.zero_check is False
    restored_value = device.current
    assert abs(restored_value - BASELINE_CURRENT) < BASELINE_TOLERANCE


def test_reconnect_command(device):
    device.command_inout("Reconnect")
    assert device.state() == DevState.ON
    assert "Reconnected" in device.status()
    value = device.current
    assert abs(value - BASELINE_CURRENT) < BASELINE_TOLERANCE


def test_concurrent_reconnect_does_not_race(device):
    """Regression test for a real race condition found and fixed live
    during Chapter 4 development: two concurrent Reconnect calls each
    independently opened a TCP connection to the adapter within 2ms of
    each other, silently orphaning one socket, before _connect_and_setup
    was made to hold _link_lock for its entire body (not just the
    setup-command portion). This test cannot directly observe the fix
    the way the live diagnostic-delay test did, but it does confirm the
    device survives concurrent Reconnect calls without raising and
    remains genuinely functional afterward."""
    d1 = tango.DeviceProxy(device.name())
    d2 = tango.DeviceProxy(device.name())

    results = {}

    def call(proxy, key):
        try:
            proxy.command_inout("Reconnect")
            results[key] = "OK"
        except Exception as e:  # noqa: BLE001
            results[key] = f"ERROR: {e}"

    t1 = threading.Thread(target=call, args=(d1, "a"))
    t2 = threading.Thread(target=call, args=(d2, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results == {"a": "OK", "b": "OK"}
    assert device.state() == DevState.ON
    value = device.current
    assert abs(value - BASELINE_CURRENT) < BASELINE_TOLERANCE


def test_zero_check_write_blocked_in_fault(device, fake_hardware):
    """Compliance-audit regression test: zero_check writes must be
    rejected via Tango's own is_zero_check_allowed guard while in
    FAULT, not silently accepted or left to an ad-hoc manual check
    inside the setter (the original implementation raised a plain
    RuntimeError from inside the setter body -- not the idiomatic
    Tango pattern established in Ch2's is_setpoint_allowed/
    is_Reset_allowed)."""
    stop_proc(fake_hardware["adapter_proc"])
    deadline = time.time() + 5
    while time.time() < deadline and device.state() != DevState.FAULT:
        time.sleep(0.05)
    assert device.state() == DevState.FAULT

    with pytest.raises(tango.DevFailed):
        device.zero_check = True


def test_invalid_gpib_address_faults_at_startup(fake_hardware):
    """Compliance-audit regression test: a real GPIB bus address is
    0-30 (5-bit primary address). An out-of-range GpibAddress must
    fault immediately at startup with a clear message, never attempt
    a connection at all -- same principle as Ch3's frame_interval_ms
    validation."""
    with (
        patch.object(faraday_cup.redis, "Redis", return_value=AsyncMock()),
        DeviceTestContext(
            faraday_cup.FaradayCup,
            properties={
                "PrologixHost": "localhost",
                "PrologixPort": fake_hardware["adapter_port"],
                "GpibAddress": 99,
                "PollIntervalMs": 100,
            },
            process=True,
        ) as proxy,
    ):
        deadline = time.time() + 3
        while time.time() < deadline and proxy.state() != DevState.FAULT:
            time.sleep(0.05)
        assert proxy.state() == DevState.FAULT
        assert "GpibAddress must be 0-30" in proxy.status()


def test_fault_and_self_heal_on_adapter_loss(device, fake_hardware):
    """Regression test for the two real bugs found and fixed live during
    Chapter 4 development: an unbounded-retry storm with no backoff,
    and an AttributeError from an unguarded None writer masking the
    real failure state. This test kills the adapter, confirms a clean
    FAULT transition, restarts the adapter, and confirms the poll loop
    self-heals back to ON with no manual Reconnect call -- the same
    scenario proven live via real timestamped logs earlier this
    chapter, now as a repeatable automated check."""
    stop_proc(fake_hardware["adapter_proc"])

    deadline = time.time() + 5
    while time.time() < deadline and device.state() != DevState.FAULT:
        time.sleep(0.05)
    assert device.state() == DevState.FAULT

    fake_hardware["adapter_proc"] = start_adapter(
        fake_hardware["adapter_port"], fake_hardware["pico_port"]
    )

    deadline = time.time() + 10
    while time.time() < deadline and device.state() != DevState.ON:
        time.sleep(0.05)
    assert device.state() == DevState.ON
    assert "polling" in device.status()

    value = device.current
    assert abs(value - BASELINE_CURRENT) < BASELINE_TOLERANCE

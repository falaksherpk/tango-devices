"""
LAB 7.9 -- compliance audit regression test: StateOne's real
Fault-handling behavior when the wrapped device is completely
unreachable (not just reporting its own FAULT state while still
running).

This test kills and restarts the REAL linac/beam/faradaycup1 device
server process -- not a disposable test element, the actual server
backing Ch4 and everything built on it since. Manually verified
first (see the class docstring in faraday_zerod_ctrl.py for the
finding this fixes): AddDevice previously succeeded silently even
with the device gone, and StateOne raised a raw
tango.ConnectionFailed instead of returning something
distinguishable. The fix wraps proxy.state()/status() in
try/except tango.DevFailed, returning State.Fault with a status
string naming the real reason.

Restart is via a detached background process, polled for real
recovery (not a fixed sleep) -- if it fails to come back within the
timeout, this test fails loudly rather than leaving the device
silently broken for whatever runs next.
"""
import subprocess
import sys
import time

import pytest
import tango

from sardana import State
from sardana_controllers.faraday_zerod_ctrl import FaradayZeroDController

DEVICE_NAME = "linac/beam/faradaycup1"
DEVICE_SCRIPT = "linac/beam/faraday_cup.py"
DEVICE_INSTANCE = "faradaycup1"


def find_real_pid():
    result = subprocess.run(
        ["pgrep", "-f", f"{DEVICE_SCRIPT} {DEVICE_INSTANCE}"],
        capture_output=True,
        text=True,
    )
    pids = [int(p) for p in result.stdout.split()]
    assert len(pids) == 1, (
        f"expected exactly one running {DEVICE_INSTANCE} process, found {pids}"
    )
    return pids[0]


def wait_for_device_state(expected_reachable, timeout=15):
    proxy = tango.DeviceProxy(DEVICE_NAME)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proxy.ping()
            reachable = True
        except tango.DevFailed:
            reachable = False
        if reachable == expected_reachable:
            return
        time.sleep(0.3)
    raise AssertionError(
        f"{DEVICE_NAME} did not become "
        f"{'reachable' if expected_reachable else 'unreachable'} "
        f"within {timeout}s"
    )


@pytest.fixture
def killed_and_restarted_faraday_cup():
    pid = find_real_pid()
    subprocess.run(["kill", str(pid)], check=True)
    wait_for_device_state(expected_reachable=False)

    yield

    subprocess.Popen(
        [sys.executable, "-u", DEVICE_SCRIPT, DEVICE_INSTANCE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_for_device_state(expected_reachable=True, timeout=20)

    # Confirm it's not just reachable but genuinely healthy again.
    proxy = tango.DeviceProxy(DEVICE_NAME)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proxy.state() == tango.DevState.ON:
            break
        time.sleep(0.3)
    assert proxy.state() == tango.DevState.ON, (
        f"{DEVICE_NAME} restarted but did not reach ON within timeout "
        f"(state={proxy.state()})"
    )


def test_state_one_returns_fault_when_device_unreachable(
    killed_and_restarted_faraday_cup,  # noqa: ARG001
):
    ctrl = FaradayZeroDController(
        "test_inst", {"TangoDevice": DEVICE_NAME}
    )
    ctrl.AddDevice(1)  # must not raise, even though the device is gone

    state, status = ctrl.StateOne(1)

    assert state == State.Fault
    assert "unreachable" in status.lower()
    assert DEVICE_NAME in status

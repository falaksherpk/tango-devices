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

Since Part 4 Ch35 the device runs as the systemd unit
tango-faradaycup@faradaycup1.service from the installed package, so the
test stops and starts that unit (Ch38, item 7) instead of killing a
process found by its old source path and starting a copy by hand, which
would have run outside systemd, as the wrong user and without the unit's
hardening. Nothing else stops with it: the Pool only Wants= and After= the
cup. It runs on tango-ds.beamline only, with passwordless sudo for
systemctl (or credentials cached by a `sudo -v` just before), and is
skipped elsewhere; CI never runs it. Recovery is polled, not slept: if the
cup isn't back ON within the timeout, the test fails loudly rather than
leaving the device down for whatever runs next.
"""

import socket
import subprocess
import time

import pytest
import tango

from sardana import State
from beamline_tango.sardana_controllers.faraday_zerod_ctrl import FaradayZeroDController

DEVICE_NAME = "linac/beam/faradaycup1"
UNIT = "tango-faradaycup@faradaycup1.service"


def systemctl(*args):
    return subprocess.run(
        ["sudo", "-n", "systemctl", *args], capture_output=True, text=True
    )


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
def stopped_and_restarted_faraday_cup():
    if not socket.gethostname().startswith("tango-ds"):
        pytest.skip("manual test: run it on tango-ds.beamline")
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
        pytest.skip("needs sudo for systemctl: run `sudo -v` first")
    active = systemctl("is-active", UNIT).stdout.strip()
    assert active == "active", f"{UNIT} is {active!r} before the test"
    stopped = systemctl("stop", UNIT)
    assert stopped.returncode == 0, stopped.stderr
    wait_for_device_state(expected_reachable=False)

    yield

    started = systemctl("start", UNIT)
    assert started.returncode == 0, started.stderr
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
    stopped_and_restarted_faraday_cup,  # noqa: ARG001
):
    ctrl = FaradayZeroDController("test_inst", {"TangoDevice": DEVICE_NAME})
    ctrl.AddDevice(1)  # must not raise, even though the device is gone

    state, status = ctrl.StateOne(1)

    assert state == State.Fault
    assert "unreachable" in status.lower()
    assert DEVICE_NAME in status

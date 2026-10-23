"""
Part 3 Chapter 3 -- DeviceTestContext pytest suite for CeYagCamera.

Mocks the hardware (a fake TCP camera generating real CEYG-framed
bytes), not the device's own read-loop/throttling logic -- the point is
testing the real GreenMode.Asyncio frame-reading and change_event
throttling against a deterministic fake camera, not real socket I/O.

process=True is required for the same reason established in Chapter
2's magnet test suite: DeviceTestContext(process=False) segfaults for
GreenMode.Asyncio devices when the test polls the device concurrently
with its own background asyncio task. This device shares that same
architecture (a background task reading frames and pushing events),
so the same finding applies here without needing to be rediscovered.

Real issue found building this: the fake hardware's should_fail flag
was originally a plain Python attribute on FakeCameraHardware. Since
process=True forks a separate OS process, the forked child gets its
own private COPY of that object at fork time -- a later mutation in
the parent test process (hw.should_fail = True) is invisible to the
child, so the device never actually saw the simulated failure. Fixed
using multiprocessing.Value, genuine shared memory that both the
parent and the forked child can see updates to.
"""

import contextlib
import multiprocessing
import struct
import time
from unittest.mock import patch

import numpy as np
import pytest
from tango import DevState, EventSubMode, EventType
from tango.test_context import DeviceTestContext

import ceyag_camera

WIDTH = ceyag_camera.WIDTH
HEIGHT = ceyag_camera.HEIGHT
MAGIC = ceyag_camera.MAGIC


class FakeCameraHardware:
    """In-memory stand-in for the real TCP camera.

    should_fail is a multiprocessing.Value, not a plain attribute --
    see the module docstring for why a plain attribute doesn't work
    across the fork boundary DeviceTestContext(process=True) creates.
    """

    def __init__(self):
        self.should_fail = multiprocessing.Value("b", 0)
        # Ch29: a camera that isn't listening yet, and a count of connects,
        # both shared with the forked server process for the same reason.
        self.refuse_connect = multiprocessing.Value("b", 0)
        self.connects = multiprocessing.Value("i", 0)

    def connect(self):
        if self.refuse_connect.value:
            raise ConnectionRefusedError("simulated: camera not listening")
        with self.connects.get_lock():
            self.connects.value += 1
        return _FakeConnection(self)


class _FakeConnection:
    """One connection to the fake camera: its own CEYG-framed stream, as
    each real TCP connection is, so the device's framing parser is really
    exercised. After shutdown()/close(), recv_exact() raises, like a real
    socket -- a worker thread still reading an old connection after
    Reconnect or Init can't take bytes from the new one."""

    def __init__(self, hw):
        self._hw = hw
        self._buffer = b""
        self._frame_number = 0
        self._closed = False

    def _generate_frame_bytes(self):
        self._frame_number += 1
        value = self._frame_number % 256
        payload = bytes([value]) * (WIDTH * HEIGHT)
        header = MAGIC + struct.pack(">I", len(payload))
        return header + payload

    def recv_exact(self, n):
        if self._closed:
            raise OSError("connection closed")
        if self._hw.should_fail.value:
            raise ConnectionError("simulated connection drop")
        time.sleep(0.01)  # keep the fake stream well below a runaway spin rate
        while len(self._buffer) < n:
            self._buffer += self._generate_frame_bytes()
        chunk, self._buffer = self._buffer[:n], self._buffer[n:]
        return chunk

    def shutdown(self, how):  # noqa: ARG002
        self._closed = True

    def close(self):
        self._closed = True


@pytest.fixture
def hw():
    return FakeCameraHardware()


@contextlib.contextmanager
def running_camera(hw):
    # `self` is required and unused: these functions replace bound methods on
    # CeYagCamera via patch.object, so their signature must match the
    # original instance methods they stand in for.
    def fake_blocking_connect(self):  # noqa: ARG001
        return hw.connect()

    def fake_blocking_recv_exact(self, n):
        return self._sock.recv_exact(n)

    with (
        patch.object(
            ceyag_camera.CeYagCamera, "_blocking_connect", fake_blocking_connect
        ),
        patch.object(
            ceyag_camera.CeYagCamera,
            "_blocking_recv_exact",
            fake_blocking_recv_exact,
        ),
        DeviceTestContext(
            ceyag_camera.CeYagCamera,
            properties={"frame_interval_ms": 100},
            process=True,  # required for GreenMode.Asyncio -- see module docstring
        ) as proxy,
    ):
        yield proxy


@pytest.fixture
def device(hw):
    with running_camera(hw) as proxy:
        yield proxy


def test_initial_state_is_on(device):
    assert device.state() == DevState.ON


def test_image_shape_and_dtype(device):
    frame = device.image
    assert frame.dtype == np.uint8
    assert frame.shape == (HEIGHT, WIDTH)


def test_frame_count_increases(device):
    count1 = device.frame_count
    time.sleep(0.3)
    count2 = device.frame_count
    assert count2 > count1


def test_change_event_throttling(device):
    """Deterministic version of LAB 3.4's manual throttle verification:
    subscribe to real change_events and confirm the inter-event timing
    matches frame_interval_ms, not the much faster fake frame rate."""
    received = []

    def callback(event):
        if not event.err:
            received.append(time.monotonic())

    device.subscribe_event(
        "image", EventType.CHANGE_EVENT, callback, sub_mode=EventSubMode.Stateless
    )
    time.sleep(1.0)

    # First event fires immediately on subscription (Tango's own behavior,
    # confirmed in an isolated sandbox test) -- exclude it from the
    # inter-event timing measurement, which only concerns genuine pushes.
    deltas_ms = [
        (received[i] - received[i - 1]) * 1000 for i in range(2, len(received))
    ]

    assert len(deltas_ms) >= 3, (
        f"expected several throttled events in 1s, got {len(received)} total"
    )
    median_delta = sorted(deltas_ms)[len(deltas_ms) // 2]
    assert 80 <= median_delta <= 150, (
        f"expected inter-event delta near 100ms (frame_interval_ms), "
        f"got median {median_delta:.1f}ms across {deltas_ms}"
    )

    # The throttle must actually be skipping frames, not just coincidentally
    # matching -- frame_count should have advanced far more than the number
    # of events actually pushed.
    assert device.frame_count > len(received) * 2


def test_reconnect_recovers_from_fault(device, hw):
    hw.should_fail.value = 1
    deadline = time.time() + 3
    while time.time() < deadline and device.state() != DevState.FAULT:
        time.sleep(0.02)
    assert device.state() == DevState.FAULT
    assert "Camera connection lost" in device.status()

    hw.should_fail.value = 0
    device.Reconnect()
    deadline = time.time() + 3
    while time.time() < deadline and device.state() != DevState.ON:
        time.sleep(0.02)
    assert device.state() == DevState.ON


def test_init_fault_on_invalid_frame_interval():
    with (
        patch.object(
            ceyag_camera.CeYagCamera,
            "_blocking_connect",
            lambda self: FakeCameraHardware().connect(),  # noqa: ARG005
        ),
        DeviceTestContext(
            ceyag_camera.CeYagCamera,
            properties={"frame_interval_ms": -1},
            process=True,
        ) as proxy,
    ):
        deadline = time.time() + 3
        while time.time() < deadline and proxy.state() != DevState.FAULT:
            time.sleep(0.02)
        assert proxy.state() == DevState.FAULT
        assert "frame_interval_ms must be positive" in proxy.status()


def test_init_leaves_exactly_one_read_loop(device):
    # Init runs delete_device() then init_device(), which connects and starts
    # a new read loop. An old loop that survived Init would read the new
    # connection too (it uses self._sock): two loops splitting one stream
    # break the framing and the device faults, or frames are counted twice.
    def rate(seconds=1.0):
        start = device.frame_count
        time.sleep(seconds)
        return (device.frame_count - start) / seconds

    before = rate()
    device.Init()
    time.sleep(0.2)
    after = rate()
    assert device.state() == DevState.ON
    assert after == pytest.approx(before, rel=0.4), (
        f"frame rate {before:.0f}/s before Init, {after:.0f}/s after"
    )


def wait_for_state(proxy, state, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline and proxy.state() != state:
        time.sleep(0.05)
    return proxy.state()


def test_heals_when_the_camera_stream_returns(device, hw):
    # The camera drops the stream, then serves again (a power cycle, or its
    # simulator unit restarting): the device must return to ON by itself,
    # with no Reconnect (Ch29). Before Ch29 it stayed FAULT.
    hw.should_fail.value = 1
    assert wait_for_state(device, DevState.FAULT, 3) == DevState.FAULT
    time.sleep(1.0)
    hw.should_fail.value = 0
    assert wait_for_state(device, DevState.ON, 5) == DevState.ON
    assert device.status() == "Connected, streaming frames."
    count = device.frame_count
    time.sleep(0.3)
    assert device.frame_count > count


def test_starts_in_fault_then_recovers_when_the_camera_appears(hw):
    # Started before its camera: FAULT, then ON by itself once the camera
    # listens. The read loop runs even when the first connect fails (Ch29).
    hw.refuse_connect.value = 1
    with running_camera(hw) as proxy:
        assert proxy.state() == DevState.FAULT
        assert "Failed to connect to camera" in proxy.status()
        hw.refuse_connect.value = 0
        assert wait_for_state(proxy, DevState.ON, 5) == DevState.ON


def test_backs_off_against_a_camera_that_accepts_but_cannot_stream(device, hw):
    # Every connect succeeds and every read fails. The device must keep
    # redialling, but back off: a connect is no evidence the link works, so
    # only a received frame resets the backoff (the Faraday cup's lesson, Ch26).
    # With a 0.5 s start, doubling: about 3 connects in 4 s; resetting on each
    # connect would be about 8.
    hw.should_fail.value = 1
    assert wait_for_state(device, DevState.FAULT, 3) == DevState.FAULT
    start = hw.connects.value
    time.sleep(4)
    n = hw.connects.value - start
    assert 2 <= n <= 5, f"{n} connects in 4 s"


def test_reconnect_reports_its_own_failure(device, hw):
    # Already FAULT from a lost stream, then a Reconnect that can't connect:
    # the status must give the new reason, not keep the old one (Ch29).
    hw.should_fail.value = 1
    assert wait_for_state(device, DevState.FAULT, 3) == DevState.FAULT
    assert "Camera connection lost" in device.status()
    hw.refuse_connect.value = 1
    device.Reconnect()
    assert device.state() == DevState.FAULT
    assert "Failed to connect to camera" in device.status()

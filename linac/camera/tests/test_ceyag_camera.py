"""
Part 3 Chapter 3 -- DeviceTestContext pytest suite for CeYagCamera.

Mocks hardware (a fake TCP camera generating real CEYG-framed bytes)
and Redis, not the device's own read-loop/throttling logic -- the
point is testing the real GreenMode.Asyncio frame-reading and
change_event throttling against a deterministic fake camera, not real
socket I/O or a real Redis server.

process=True is required for the same reason established in Chapter
2's magnet test suite: DeviceTestContext(process=False) segfaults for
GreenMode.Asyncio devices when the test polls the device concurrently
with its own background asyncio task. This device shares that same
architecture (a background task reading frames and pushing events),
so the same finding applies here without needing to be rediscovered.

Following Chapter 2's own precedent: redis.Redis is mocked purely to
avoid a real Redis dependency in CI. Because process=True forks a
separate OS process, any mock's recorded calls live in that forked
child's private memory and are not observable from the test process --
so, matching Ch2, this suite does not assert anything about what the
mock's .xadd() received. LAB 3.4's manual live verification (redis-cli
XRANGE, confirmed against real timing math) is the record that
archiving actually works; this suite only prevents a hard dependency.

Real issue found building this: the fake hardware's should_fail flag
was originally a plain Python attribute on FakeCameraHardware. Since
process=True forks a separate OS process, the forked child gets its
own private COPY of that object at fork time -- a later mutation in
the parent test process (hw.should_fail = True) is invisible to the
child, so the device never actually saw the simulated failure. Fixed
using multiprocessing.Value, genuine shared memory that both the
parent and the forked child can see updates to.
"""
import multiprocessing
import struct
import time
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from tango import DevState, EventType
from tango.test_context import DeviceTestContext

import ceyag_camera

WIDTH = ceyag_camera.WIDTH
HEIGHT = ceyag_camera.HEIGHT
MAGIC = ceyag_camera.MAGIC


class FakeCameraHardware:
    """In-memory stand-in for the real TCP camera. Generates real
    CEYG-framed byte streams on demand, so the device's own framing
    parser is genuinely exercised, not bypassed.

    should_fail is a multiprocessing.Value, not a plain attribute --
    see the module docstring for why a plain attribute doesn't work
    across the fork boundary DeviceTestContext(process=True) creates.
    """

    def __init__(self):
        self._buffer = b""
        self._frame_number = 0
        self.should_fail = multiprocessing.Value("b", 0)

    def _generate_frame_bytes(self):
        self._frame_number += 1
        value = self._frame_number % 256
        payload = bytes([value]) * (WIDTH * HEIGHT)
        header = MAGIC + struct.pack(">I", len(payload))
        return header + payload

    def recv_exact(self, n):
        if self.should_fail.value:
            raise ConnectionError("simulated connection drop")
        time.sleep(0.01)  # keep the fake stream well below a runaway spin rate
        while len(self._buffer) < n:
            self._buffer += self._generate_frame_bytes()
        chunk, self._buffer = self._buffer[:n], self._buffer[n:]
        return chunk


class _FakeSocketHandle:
    """Stands in for a real socket object -- only needs a no-op close()."""

    def close(self):
        pass


@pytest.fixture
def hw():
    return FakeCameraHardware()


@pytest.fixture
def device(hw):
    # `self` is required and unused: these functions replace bound methods on
    # CeYagCamera via patch.object, so their signature must match the
    # original instance methods they stand in for.
    def fake_blocking_connect(self):  # noqa: ARG001
        return _FakeSocketHandle()

    def fake_blocking_recv_exact(self, n):  # noqa: ARG001
        return hw.recv_exact(n)

    with (
        patch.object(
            ceyag_camera.CeYagCamera, "_blocking_connect", fake_blocking_connect
        ),
        patch.object(
            ceyag_camera.CeYagCamera,
            "_blocking_recv_exact",
            fake_blocking_recv_exact,
        ),
        patch.object(ceyag_camera.redis, "Redis", return_value=AsyncMock()),
        DeviceTestContext(
            ceyag_camera.CeYagCamera,
            properties={"frame_interval_ms": 100},
            process=True,  # required for GreenMode.Asyncio -- see module docstring
        ) as proxy,
    ):
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
        "image", EventType.CHANGE_EVENT, callback, stateless=True
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
            lambda self: _FakeSocketHandle(),  # noqa: ARG005
        ),
        patch.object(ceyag_camera.redis, "Redis", return_value=AsyncMock()),
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

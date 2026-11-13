"""tests/tango_context.py: a failed start leaves no server; a refused one is retried.

Each test kills any server it left in its finally, so it fails rather than hangs
when run against PyTango's own classes (the fail-before, handbook Part 4, Ch43).
"""

import time

import pytest
import tango
from tango.server import Device, attribute
from tango_context import DeviceTestContext

REAL_PING = tango.DeviceProxy.ping


class Tiny(Device):
    @attribute(dtype=int)
    def answer(self):
        return 42


def _throw(reason):
    tango.Except.throw_exception(reason, f"injected {reason}", "test_tango_context")


def _kill_leftover(thread):
    if thread.is_alive():
        thread.kill()
        thread.join(5)


def test_a_failed_start_leaves_no_server(monkeypatch):
    def ping(_self, *_args, **_kwargs):
        _throw("API_Other")

    monkeypatch.setattr(tango.DeviceProxy, "ping", ping)
    ctx = DeviceTestContext(Tiny, process=True)
    t0 = time.monotonic()
    try:
        with pytest.raises(tango.DevFailed), ctx:
            pass
        assert not ctx.thread.is_alive(), "the failed start left its server running"
        assert time.monotonic() - t0 < 20
    finally:
        _kill_leftover(ctx.thread)


def test_a_refused_start_is_retried_once(monkeypatch):
    first = {}

    def ping(self, *args, **kwargs):
        if not first:
            first["thread"] = ctx.thread
            _throw("API_CantConnectToDevice")
        return REAL_PING(self, *args, **kwargs)

    monkeypatch.setattr(tango.DeviceProxy, "ping", ping)
    ctx = DeviceTestContext(Tiny, process=True)
    try:
        with pytest.warns(UserWarning, match="retrying once"), ctx as proxy:
            assert proxy.answer == 42
        assert not first["thread"].is_alive(), (
            "the refused start left its server running"
        )
    finally:
        _kill_leftover(first.get("thread", ctx.thread))
        _kill_leftover(ctx.thread)


def test_a_start_refused_twice_fails_and_leaves_no_server(monkeypatch):
    threads = []

    def ping(_self, *_args, **_kwargs):
        threads.append(ctx.thread)
        _throw("API_CantConnectToDevice")

    monkeypatch.setattr(tango.DeviceProxy, "ping", ping)
    ctx = DeviceTestContext(Tiny, process=True)
    try:
        warns = pytest.warns(UserWarning, match="retrying once")
        with warns, pytest.raises(tango.DevFailed), ctx:
            pass
        assert len(threads) == 2, f"{len(threads)} starts, want 2"
        assert not any(t.is_alive() for t in threads), "a refused start left a server"
    finally:
        for t in threads:
            _kill_leftover(t)
        _kill_leftover(ctx.thread)

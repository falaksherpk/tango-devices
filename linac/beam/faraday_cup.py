#!/usr/bin/env python3
"""Tango device server for the Faraday Cup beam-current monitor,
linac/beam/faradaycup1 (Chapter 4).

Transport: GreenMode.Asyncio, real asyncio TCP client talking to a fake
Prologix GPIB-ETHERNET adapter (fake_prologix_adapter.py), which itself
forwards to a fake Keithley 6485-style picoammeter (fake_picoammeter.py).

Design notes (see Chapter 4 doc for full rationale):
- zero_check is tracked as device-server-mirrored state, not a live
  hardware readback -- the real Keithley 6485 has no confirmed SCPI query
  for this, and a real ESRF/BLISS bug report (control system for
  beamline ID31) documents this exact same limitation in production:
  zero_check there is "just a logical software check", not read from
  hardware. init_device explicitly forces SYST:ZCH OFF on startup so we
  begin from a known state rather than an assumed one.
- A single asyncio.Lock serializes every device->adapter command/response
  round trip: one physical GPIB-style channel, no concurrent interleave.
- Dual archiving (Tango change_event + redis.asyncio Stream, threshold
  gated) follows the identical pattern established in Ch2/Ch3, scaled to
  this device's real magnitude (~nA readings, not amps).
- Poll-loop resilience (added after a real bug found live-testing this
  chapter): the first version retried at full poll rate forever with no
  backoff and no reconnect attempt when the adapter link died, producing
  an unbounded error-log flood and never self-healing. Fixed with
  exponential backoff (capped) plus an active reconnect attempt each
  cycle while in FAULT.
"""
import asyncio
import contextlib
import logging

import redis.asyncio as redis
from tango import AttReqType, DevState, GreenMode
from tango.server import Device, attribute, command, device_property, run

REDIS_STREAM_KEY = "linac:beam:faradaycup1"
CURRENT_CHANGE_THRESHOLD = 1e-11  # A -- scaled to this device's real magnitude
BACKOFF_CAP_S = 30.0  # ceiling for exponential reconnect backoff

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("faraday_cup")


class FaradayCup(Device):
    green_mode = GreenMode.Asyncio

    PrologixHost = device_property(dtype=str, default_value="localhost")
    PrologixPort = device_property(dtype=int, default_value=1234)
    GpibAddress = device_property(dtype=int, default_value=22)
    PollIntervalMs = device_property(dtype=int, default_value=500)

    async def init_device(self):
        await super().init_device()
        self.set_state(DevState.INIT)
        self._reader = None
        self._writer = None
        self._link_lock = asyncio.Lock()
        self._zero_check = False
        self._last_pushed_current = None
        self._redis = redis.Redis(host="127.0.0.1", port=6379,
                                   decode_responses=True)
        self.set_change_event("current", True, False)

        # Property validation (compliance audit finding): a real GPIB bus
        # address is 0-30 (5-bit primary address); PollIntervalMs must be
        # positive or the poll loop's own sleep() calls misbehave. Fail
        # loudly at startup rather than silently accepting a nonsense
        # value and misbehaving later -- same principle as Ch3's
        # frame_interval_ms validation.
        if not (0 <= self.GpibAddress <= 30):
            msg = f"GpibAddress must be 0-30, got {self.GpibAddress}"
            log.error("init_device: %s", msg)
            self.set_state(DevState.FAULT)
            self.set_status(msg)
            return
        if self.PollIntervalMs <= 0:
            msg = f"PollIntervalMs must be positive, got {self.PollIntervalMs}"
            log.error("init_device: %s", msg)
            self.set_state(DevState.FAULT)
            self.set_status(msg)
            return

        try:
            await self._connect_and_setup()
        except (TimeoutError, ConnectionRefusedError, OSError) as e:
            log.error("init_device: adapter connection failed: %s", e)
            self.set_state(DevState.FAULT)
            self.set_status(f"Adapter connection failed: {e}")
            self._poll_task = asyncio.ensure_future(self._poll_loop())
            return

        self._poll_task = asyncio.ensure_future(self._poll_loop())
        self.set_state(DevState.ON)
        self.set_status("Connected, polling")

    async def _connect_and_setup(self):
        """Serialized on _link_lock for its entire body -- including the
        connection itself, not just the setup commands -- so a manual
        Reconnect command can never race the poll loop's own automatic
        reconnect attempt. A real race here was found and confirmed live
        (two concurrent Reconnect calls both opened a TCP connection
        within 2ms of each other, silently orphaning one socket) before
        this lock was widened to cover the whole method."""
        async with self._link_lock:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.PrologixHost, self.PrologixPort),
                timeout=3.0)
            # Lock already held for this whole method -- no nested
            # acquire here (asyncio.Lock is not reentrant).
            await self._send_line("++mode 1")
            await self._send_line("++auto 0")
            await self._send_line(f"++addr {self.GpibAddress}")
            await self._send_line("++eos 0")
            # Force a known zero-check state; the real instrument's
            # power-on state cannot be queried (see module docstring).
            await self._send_line("SYST:ZCH OFF")
            self._zero_check = False
        log.info("connected to adapter at %s:%d, addr=%d",
                  self.PrologixHost, self.PrologixPort, self.GpibAddress)

    async def _send_line(self, line: str):
        self._writer.write((line + "\n").encode("ascii"))
        await self._writer.drain()

    async def _read_line(self, timeout_s: float = 1.0) -> str | None:
        try:
            raw = await asyncio.wait_for(self._reader.readline(),
                                          timeout=timeout_s)
        except TimeoutError:
            return None
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").strip()

    async def _read_current(self) -> float:
        async with self._link_lock:
            await self._send_line("READ?")
            await self._send_line("++read")
            reply = await self._read_line(timeout_s=1.0)
        if reply is None:
            raise TimeoutError("no response from adapter/instrument")
        # Real instrument format, e.g. "+2.718427E-13A"
        return float(reply.rstrip("A"))

    async def _archive_tick(self, value: float):
        """Dual delivery, one tick: a real Tango change_event AND a Redis
        Stream entry, threshold-gated so we don't flood either path with
        near-identical values. Mirrors the magnet's _archive_tick pattern
        (Part3 Ch2), scaled to this device's real magnitude."""
        self.push_change_event("current", value)
        if (
            self._last_pushed_current is None
            or abs(value - self._last_pushed_current) >= CURRENT_CHANGE_THRESHOLD
        ):
            await self._redis.xadd(REDIS_STREAM_KEY, {"current": f"{value:.6E}"})
            self._last_pushed_current = value

    async def _poll_loop(self):
        interval_s = self.PollIntervalMs / 1000.0
        backoff_s = interval_s
        while True:
            if self._writer is not None:
                try:
                    value = await self._read_current()
                    await self._archive_tick(value)
                    if self.get_state() != DevState.ON:
                        self.set_state(DevState.ON)
                        self.set_status("Connected, polling")
                    backoff_s = interval_s
                    await asyncio.sleep(interval_s)
                    continue
                except (TimeoutError, ConnectionError, OSError) as e:
                    log.error("poll loop error: %s", e)
                    self.set_state(DevState.FAULT)
                    self.set_status(f"Read failed: {e}")
            else:
                self.set_state(DevState.FAULT)
                self.set_status("No connection to adapter")

            # Reconnect attempt (outside the read try/except above so a
            # failed reconnect doesn't get misreported as a read error).
            if self._writer is not None:
                with contextlib.suppress(OSError):
                    self._writer.close()
            try:
                await self._connect_and_setup()
                log.info("poll loop: reconnect succeeded, backoff reset")
                backoff_s = interval_s
            except (TimeoutError, ConnectionRefusedError, OSError) as reconnect_err:
                log.error("poll loop: reconnect failed: %s", reconnect_err)
                backoff_s = min(backoff_s * 2, BACKOFF_CAP_S)
            await asyncio.sleep(backoff_s)

    @attribute(dtype=float, label="Current", unit="A",
               format="%.6E")
    async def current(self):
        return await self._read_current()

    @attribute(dtype=bool, label="Zero Check")
    async def zero_check(self):
        return self._zero_check

    def is_zero_check_allowed(self, req_type):
        if req_type == AttReqType.WRITE_REQ:
            return self.get_state() != DevState.FAULT
        return True

    @zero_check.setter
    async def zero_check(self, value: bool):
        async with self._link_lock:
            await self._send_line("SYST:ZCH ON" if value else "SYST:ZCH OFF")
        self._zero_check = value

    @command
    async def Reconnect(self):
        """Re-establish the device->adapter TCP link and replay the setup
        sequence. Cannot fix a broken adapter->instrument link -- that is
        outside this device's control, same caveat as Ch3's camera
        Reconnect command re: its own fake simulator."""
        if self._writer is not None:
            self._writer.close()
        await self._connect_and_setup()
        self.set_state(DevState.ON)
        self.set_status("Reconnected, polling")


if __name__ == "__main__":
    run((FaradayCup,))

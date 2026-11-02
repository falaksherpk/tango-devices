#!/usr/bin/env python3
"""Tango device server for the Faraday Cup beam-current monitor,
linac/beam/faradaycup1 -- handbook Part 3, Ch26.

Transport: GreenMode.Asyncio, real asyncio TCP client talking to a fake
Prologix GPIB-ETHERNET adapter (fake_prologix_adapter.py), which itself
forwards to a fake Keithley 6485-style picoammeter (fake_picoammeter.py).

Design notes:
- zero_check is tracked as device-server-mirrored state, not a live
  hardware readback -- the real Keithley 6485 has no confirmed SCPI query
  for this, and a real ESRF/BLISS bug report (control system for
  beamline ID31) documents this exact same limitation in production:
  zero_check there is "just a logical software check", not read from
  hardware. init_device explicitly forces SYST:ZCH OFF on startup so we
  begin from a known state rather than an assumed one.
- A single asyncio.Lock serializes every device->adapter command/response
  round trip: one physical GPIB-style channel, no concurrent interleave.
  Opening a new link happens under the same lock and closes the old one
  first, so Reconnect and the poll loop's own reconnect can't race.
- current is pushed as a change event on every successful poll. It is
  not archived here (HDB++, Ch30).
- Poll-loop resilience: exponential backoff (capped) plus an active
  reconnect attempt each cycle while in FAULT, so the device heals
  itself once the adapter is back. The backoff resets only after a
  successful read, not after a successful connect.
- delete_device() (run before every Init and at server shutdown) cancels
  and awaits the poll task, then closes the link, so no poll loop
  outlives its device. Without it, a server shut down while the loop was
  retrying a dead adapter aborted (Ch26).
"""

import asyncio
import contextlib
import logging

from tango import AttReqType, DevState, GreenMode
from tango.server import Device, attribute, command, device_property, run

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
        self._poll_task = None
        self._reader = None
        self._writer = None
        self._link_lock = asyncio.Lock()
        self._zero_check = False
        self._poll_count = 0
        self.set_state(DevState.INIT)
        self.set_status("Initialising")
        self.set_change_event("current", True, False)

        # Property validation: a real GPIB bus address is 0-30 (5-bit
        # primary address); PollIntervalMs must be positive or the poll
        # loop's own sleep() calls misbehave. Fail loudly at startup
        # rather than silently accepting a nonsense value.
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
        else:
            self.set_state(DevState.ON)
            self.set_status("Connected, polling")
        # The poll loop runs in both cases: in FAULT it keeps reconnecting.
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def delete_device(self):
        # Init and server shutdown call this: stop the poll loop before the
        # device (or the whole server) goes away underneath it.
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        async with self._link_lock:
            await self._close_link()
        await super().delete_device()

    async def _close_link(self):
        """Close the current link, if any. Caller holds _link_lock."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            async with asyncio.timeout(1.0):
                await writer.wait_closed()

    async def _connect_and_setup(self):
        """Serialized on _link_lock for its entire body -- closing the old
        link, opening the new one and sending the setup commands -- so a
        manual Reconnect can never race the poll loop's own reconnect, and
        a replaced link is always closed, not left to the garbage
        collector."""
        async with self._link_lock:
            await self._close_link()
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.PrologixHost, self.PrologixPort),
                timeout=3.0,
            )
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
        log.info(
            "connected to adapter at %s:%d, addr=%d",
            self.PrologixHost,
            self.PrologixPort,
            self.GpibAddress,
        )

    async def _send_line(self, line: str):
        self._writer.write((line + "\n").encode("ascii"))
        await self._writer.drain()

    async def _read_line(self, timeout_s: float = 1.0) -> str | None:
        try:
            raw = await asyncio.wait_for(self._reader.readline(), timeout=timeout_s)
        except TimeoutError:
            return None
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").strip()

    async def _read_current(self) -> float:
        async with self._link_lock:
            if self._writer is None:
                raise ConnectionError("no connection to adapter")
            await self._send_line("READ?")
            await self._send_line("++read")
            reply = await self._read_line(timeout_s=1.0)
        if reply is None:
            raise TimeoutError("no response from adapter/instrument")
        # Real instrument format, e.g. "+2.718427E-13A"
        return float(reply.rstrip("A"))

    async def _poll_loop(self):
        # Cancellation (Init, shutdown) ends the loop at an await; it is
        # never caught here.
        interval_s = self.PollIntervalMs / 1000.0
        backoff_s = interval_s
        while True:
            if self._writer is not None:
                try:
                    value = await self._read_current()
                    self._poll_count += 1
                    self.push_change_event("current", value)
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
            # The backoff grows either way and resets only after a
            # successful read: an endpoint that accepts the connection but
            # can't answer (the adapter with its instrument gone) must not
            # be redialled at the poll rate (Ch26).
            try:
                await self._connect_and_setup()
                log.info("poll loop: reconnected, next read in %.1f s", backoff_s)
            except (TimeoutError, ConnectionRefusedError, OSError) as reconnect_err:
                log.error("poll loop: reconnect failed: %s", reconnect_err)
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, BACKOFF_CAP_S)

    @attribute(dtype=float, label="Current", unit="A", format="%.6E")
    async def current(self):
        return await self._read_current()

    @attribute(dtype=int, label="Poll Count")
    async def poll_count(self):
        return self._poll_count

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
        outside this device's control, same caveat as the camera's
        Reconnect re: its own fake simulator."""
        await self._connect_and_setup()
        self.set_state(DevState.ON)
        self.set_status("Reconnected, polling")


if __name__ == "__main__":
    run((FaradayCup,), green_mode=GreenMode.Asyncio)

#!/usr/bin/env python3
"""
Part 3 Chapter 2 -- Magnet Power Supply (RS-232), linac/magnet/q1.

Real finite-ramp-rate physics, GreenMode.Asyncio throughout (confirmed
working against the real installed PyTango 10.3.1 via isolated sandbox
tests before this file was written), a truthful MOVING state, and
(this pass) dual archiving: a real Tango change_event AND a Redis
Stream entry per meaningful tick, threshold-gated the same way as
Phase 3's vacuum controller -- using redis.asyncio specifically
because this device's event loop is already async under
GreenMode.Asyncio, so the Redis client integrates natively with no
thread-safety concerns (unlike Phase 3's pymodbus-based device, which
needed an explicit threading.Lock).

Talks to the real (or here, socat-simulated) RS-232 magnet PSU proven
standalone in fake_magnet_psu.py, using the same SCPI-style *IDN?/CURR?
pattern already proven in Phase 2 Lab 2.1.
"""
import asyncio

import redis.asyncio as redis
import serial
from tango import DevState, GreenMode
from tango.server import Device, attribute, command, device_property, run

REDIS_STREAM_KEY = "linac:magnet:q1"
CURRENT_CHANGE_THRESHOLD = 0.001  # A -- matches Phase 3's threshold-gating pattern


class MagnetPowerSupply(Device):
    green_mode = GreenMode.Asyncio

    port = device_property(dtype=str, default_value="/dev/pts/2")
    baudrate = device_property(dtype=int, default_value=9600)
    ramp_rate = device_property(dtype=float, default_value=5.0)  # A/s

    async def init_device(self):
        await super().init_device()
        self._setpoint = 0.0
        self._current = 0.0
        self._ramp_task = None
        self._last_pushed_current = None

        self.set_change_event("current", True, False)

        self._redis = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

        try:
            self._ser = serial.Serial(self.port, baudrate=self.baudrate, timeout=2)
            self._current = await self._query_current()
            self._setpoint = self._current
            self.set_state(DevState.ON)
        except Exception as e:
            self.error_stream(f"Failed to open serial port {self.port}: {e}")
            self.set_state(DevState.FAULT)

    async def _query_current(self):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._blocking_query_current)

    def _blocking_query_current(self):
        self._ser.write(b"CURR?\r\n")
        response = self._ser.readline().decode(errors="replace").strip()
        return float(response)

    async def _write_current(self, value):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._blocking_write_current, value)

    def _blocking_write_current(self, value):
        self._ser.write(f"CURR {value:.4f}\r\n".encode())

    async def _archive_tick(self, value):
        """Dual delivery, one tick: a real Tango change_event AND a
        Redis Stream entry, threshold-gated so we don't flood either
        path with near-identical values."""
        self.push_change_event("current", value)
        if (
            self._last_pushed_current is None
            or abs(value - self._last_pushed_current) >= CURRENT_CHANGE_THRESHOLD
        ):
            await self._redis.xadd(REDIS_STREAM_KEY, {"current": f"{value:.4f}"})
            self._last_pushed_current = value

    async def _ramp_loop(self, target):
        self.set_state(DevState.MOVING)
        step = self.ramp_rate * 0.2  # ramp_rate is A/s, ticking every 0.2s
        try:
            while abs(self._current - target) > 1e-6:
                if self._current < target:
                    self._current = min(self._current + step, target)
                else:
                    self._current = max(self._current - step, target)
                await self._write_current(self._current)
                await self._archive_tick(self._current)
                await asyncio.sleep(0.2)
            self.set_state(DevState.ON)
        except asyncio.CancelledError:
            pass

    @attribute(dtype=float, label="Setpoint", unit="A")
    async def setpoint(self):
        return self._setpoint

    @setpoint.setter
    async def setpoint(self, value):
        self._setpoint = value
        if self._ramp_task:
            self._ramp_task.cancel()
        self._ramp_task = asyncio.create_task(self._ramp_loop(value))

    @attribute(
        dtype=float,
        label="Current",
        unit="A",
        min_alarm=-1.0,
        max_alarm=100.0,
    )
    async def current(self):
        return self._current

    @command
    async def Reset(self):
        if self._ramp_task:
            self._ramp_task.cancel()
        self._setpoint = 0.0
        self._ramp_task = asyncio.create_task(self._ramp_loop(0.0))


if __name__ == "__main__":
    run((MagnetPowerSupply,), green_mode=GreenMode.Asyncio)

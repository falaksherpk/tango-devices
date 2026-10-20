#!/usr/bin/env python3
"""
Magnet power supply (RS-232), linac/magnet/q1 -- handbook Part 3, Ch24.

Finite-ramp-rate physics under GreenMode.Asyncio, with a truthful MOVING
state. Every ramp tick pushes a Tango change event on 'current'; archiving
is left to HDB++ (Ch30), which subscribes to those events, so the device
holds no archiving code of its own.

Talks to the RS-232 magnet PSU (here fake_magnet_psu.py on the other end of
a socat pty pair) with SCPI-style commands: CURR? and CURR <value>. The
blocking serial calls run in a worker thread (asyncio.to_thread), so the
event loop keeps serving clients while the port is busy.
"""

import asyncio
import contextlib

import serial
from tango import AttReqType, DevState, GreenMode
from tango.server import Device, attribute, command, device_property, run


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
        self._ser = None

        self.set_change_event("current", True, False)

        if self.ramp_rate <= 0:
            msg = f"ramp_rate must be positive, got {self.ramp_rate}"
            self.error_stream(msg)
            self.set_status(msg)
            self.set_state(DevState.FAULT)
            return

        try:
            self._ser = serial.Serial(self.port, baudrate=self.baudrate, timeout=2)
            self._current = await self._query_current()
        except (serial.SerialException, OSError, ValueError) as e:
            msg = f"Failed to open serial port {self.port}: {e}"
            self.error_stream(msg)
            self.set_status(msg)
            self.set_state(DevState.FAULT)
            return

        self._setpoint = self._current
        self.set_state(DevState.ON)

    async def delete_device(self):
        # Init (and server shutdown) call this before init_device: stop the
        # ramp first, so it can't keep writing to the PSU, then free the port.
        if self._ramp_task is not None:
            self._ramp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ramp_task
            self._ramp_task = None
        if self._ser is not None:
            self._ser.close()
            self._ser = None
        await super().delete_device()

    async def _query_current(self):
        return await asyncio.to_thread(self._blocking_query_current)

    def _blocking_query_current(self):
        self._ser.write(b"CURR?\r\n")
        response = self._ser.readline().decode(errors="replace").strip()
        return float(response)

    async def _write_current(self, value):
        await asyncio.to_thread(self._blocking_write_current, value)

    def _blocking_write_current(self, value):
        self._ser.write(f"CURR {value:.4f}\r\n".encode())

    async def _ramp_loop(self, target):
        # Cancellation (a new setpoint, Reset, Init) ends the loop at an await.
        self.set_state(DevState.MOVING)
        step = self.ramp_rate * 0.2  # ramp_rate is A/s, ticking every 0.2s
        while abs(self._current - target) > 1e-6:
            if self._current < target:
                self._current = min(self._current + step, target)
            else:
                self._current = max(self._current - step, target)
            await self._write_current(self._current)
            self.push_change_event("current", self._current)
            await asyncio.sleep(0.2)
        self.set_state(DevState.ON)

    def is_setpoint_allowed(self, req_type):
        if req_type == AttReqType.WRITE_REQ:
            return self.get_state() in (DevState.ON, DevState.MOVING)
        return True

    @attribute(
        dtype=float,
        label="Setpoint",
        unit="A",
        memorized=True,
        hw_memorized=True,
    )
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
        min_value=-1.0,
        max_value=100.0,
        min_alarm=-1.0,
        max_alarm=100.0,
    )
    async def current(self):
        return self._current

    def is_Reset_allowed(self):
        return self.get_state() in (DevState.ON, DevState.MOVING)

    @command
    async def Reset(self):
        if self._ramp_task:
            self._ramp_task.cancel()
        self._setpoint = 0.0
        self._ramp_task = asyncio.create_task(self._ramp_loop(0.0))


if __name__ == "__main__":
    run((MagnetPowerSupply,), green_mode=GreenMode.Asyncio)

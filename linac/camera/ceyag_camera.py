#!/usr/bin/env python3
"""
Ce:YAG camera (TCP/IP), linac/camera/ceyag1 -- handbook Part 3, Ch25.

GreenMode.Asyncio. Connects to a TCP camera (here fake_ceyag_camera.py)
speaking a simple binary framing protocol: a 4-byte b"CEYG" magic, a
4-byte big-endian length prefix, and raw grayscale pixel bytes.

The camera streams frames continuously and cannot be throttled at the
source -- frame_interval_ms instead throttles how often this device
pushes a Tango change_event for `image`, not how often frames are read
off the socket (every frame is still read, to keep the TCP receive
buffer drained; only the push is gated). The image is not archived.

IMAGE attribute dtype is declared explicitly as numpy.uint8 -- PyTango
silently promotes to int64 (8x the memory) if a bare Python `int` is
used instead.

The blocking socket calls run in a worker thread (asyncio.to_thread).
_disconnect() stops the read loop and wakes a recv() blocked in that
thread (shutdown, then close) before Reconnect, Init or server shutdown
go on, so no read loop outlives its connection or its device.
"""

import asyncio
import contextlib
import socket
import struct
import time

import numpy as np
from tango import DevState, GreenMode
from tango.server import Device, attribute, command, device_property, run

WIDTH = 640
HEIGHT = 480
MAGIC = b"CEYG"


class CeYagCamera(Device):
    green_mode = GreenMode.Asyncio

    host = device_property(dtype=str, default_value="127.0.0.1")
    port = device_property(dtype=int, default_value=9999)
    frame_interval_ms = device_property(dtype=int, default_value=200)

    async def init_device(self):
        await super().init_device()
        self._frame = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
        self._frame_count = 0
        self._last_push_time = 0.0
        self._read_task = None
        self._sock = None

        self.set_change_event("image", True, False)

        if self.frame_interval_ms <= 0:
            msg = f"frame_interval_ms must be positive, got {self.frame_interval_ms}"
            self.error_stream(msg)
            self.set_status(msg)
            self.set_state(DevState.FAULT)
            return

        await self._connect()

    async def delete_device(self):
        # Init and server shutdown call this: stop the read loop before the
        # device (or the whole server) goes away underneath it.
        await self._disconnect()
        await super().delete_device()

    async def _connect(self):
        try:
            self._sock = await asyncio.to_thread(self._blocking_connect)
        except (TimeoutError, OSError) as e:
            msg = f"Failed to connect to camera at {self.host}:{self.port}: {e}"
            self.error_stream(msg)
            self.set_status(msg)
            self.set_state(DevState.FAULT)
            return

        self.set_state(DevState.ON)
        self.set_status("Connected, streaming frames.")
        self._read_task = asyncio.create_task(self._read_loop())

    async def _disconnect(self):
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
        if self._sock is not None:
            # shutdown() wakes a recv() blocked in the worker thread; close()
            # alone does not.
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
            self._sock.close()
            self._sock = None

    def _blocking_connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((self.host, self.port))
        return sock

    async def _recv_exact(self, n):
        return await asyncio.to_thread(self._blocking_recv_exact, n)

    def _blocking_recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed before expected bytes arrived")
            buf += chunk
        return buf

    async def _read_loop(self):
        # Cancellation (Reconnect, Init, shutdown) ends the loop at an await.
        try:
            while True:
                magic = await self._recv_exact(4)
                if magic != MAGIC:
                    raise ValueError(f"bad magic bytes: {magic!r}")
                (length,) = struct.unpack(">I", await self._recv_exact(4))
                payload = await self._recv_exact(length)

                self._frame = np.frombuffer(payload, dtype=np.uint8).reshape(
                    HEIGHT, WIDTH
                )
                self._frame_count += 1

                now = time.monotonic()
                if (now - self._last_push_time) * 1000 >= self.frame_interval_ms:
                    self.push_change_event("image", self._frame)
                    self._last_push_time = now
        except (ConnectionError, OSError, struct.error, ValueError) as e:
            msg = f"Camera connection lost: {e}"
            self.error_stream(msg)
            self.set_status(msg)
            self.set_state(DevState.FAULT)

    def is_Reconnect_allowed(self):
        return self.get_state() in (DevState.ON, DevState.FAULT)

    @command
    async def Reconnect(self):
        await self._disconnect()
        await self._connect()

    @attribute(
        dtype=((np.uint8,),),
        max_dim_x=WIDTH,
        max_dim_y=HEIGHT,
        label="Image",
    )
    async def image(self):
        return self._frame

    @attribute(dtype=int, label="Frame Count")
    async def frame_count(self):
        return self._frame_count


if __name__ == "__main__":
    run((CeYagCamera,), green_mode=GreenMode.Asyncio)

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

The read loop always runs, also when the first connect fails, and heals
the device by itself (Ch29): a lost or refused connection means FAULT,
logged once, and a redial after a backoff that starts at 0.5 s and
doubles up to 30 s. Only a received frame resets the backoff and brings
the device back to ON - a camera that accepts a connection but sends no
frames must not be redialled at full speed (the Faraday cup's lesson,
Ch26). Reconnect and Init still work; they only restart the loop sooner.
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
BACKOFF_START_S = 0.5
BACKOFF_CAP_S = 30.0
STREAMING = "Connected, streaming frames."


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
        # The read loop starts either way: after a failed connect it keeps
        # redialling, so a device started before its camera heals by itself.
        try:
            self._sock = await asyncio.to_thread(self._blocking_connect)
        except (TimeoutError, OSError) as e:
            msg = f"Failed to connect to camera at {self.host}:{self.port}: {e}"
            self._fault(msg, force=True)
        else:
            self.set_state(DevState.ON)
            self.set_status(STREAMING)
        self._read_task = asyncio.create_task(self._read_loop())

    def _fault(self, msg, *, force=False):
        # Logged once per outage: a redial that fails again changes nothing. A
        # deliberate connect (init, Reconnect) always reports its own result.
        if force or self.get_state() != DevState.FAULT:
            self.error_stream(msg)
            self.set_status(msg)
            self.set_state(DevState.FAULT)

    def _close_socket(self):
        if self._sock is not None:
            # shutdown() wakes a recv() blocked in the worker thread; close()
            # alone does not.
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
            self._sock.close()
            self._sock = None

    async def _disconnect(self):
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
        self._close_socket()

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
        backoff = BACKOFF_START_S
        while True:
            if self._sock is None:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP_S)
                try:
                    self._sock = await asyncio.to_thread(self._blocking_connect)
                except (TimeoutError, OSError):
                    continue
            try:
                frame = await self._read_frame()
            except (ConnectionError, OSError, struct.error, ValueError) as e:
                self._fault(f"Camera connection lost: {e}")
                self._close_socket()
                continue

            # A frame, not a connect, is the evidence that the link works.
            backoff = BACKOFF_START_S
            self._frame = frame
            self._frame_count += 1
            if self.get_state() != DevState.ON:
                self.info_stream("Camera stream restored")
                self.set_state(DevState.ON)
                self.set_status(STREAMING)

            now = time.monotonic()
            if (now - self._last_push_time) * 1000 >= self.frame_interval_ms:
                self.push_change_event("image", self._frame)
                self._last_push_time = now

    async def _read_frame(self):
        magic = await self._recv_exact(4)
        if magic != MAGIC:
            raise ValueError(f"bad magic bytes: {magic!r}")
        (length,) = struct.unpack(">I", await self._recv_exact(4))
        payload = await self._recv_exact(length)
        return np.frombuffer(payload, dtype=np.uint8).reshape(HEIGHT, WIDTH)

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

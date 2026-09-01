#!/usr/bin/env python3
"""
Lab 2.2 -- Tango device server wrapping a network-attached (TCP/IP)
instrument. Hardened version: catches communication failures
explicitly, transitions device State to FAULT (link down) or ALARM
(link up, data untrustworthy), and automatically reconnects.
"""
import socket
import time

from tango import DevState
from tango.server import Device, attribute, command, device_property, run
import contextlib


class CommunicationError(Exception):
    pass


class MalformedResponseError(Exception):
    pass


class TcpDetector(Device):

    host = device_property(dtype=str, default_value="127.0.0.1")
    port = device_property(dtype=int, default_value=5025)
    reconnect_interval = device_property(dtype=float, default_value=5.0)

    def init_device(self):
        Device.init_device(self)
        self._device_id = "UNKNOWN"
        self._sock = None
        self._last_reconnect_attempt = 0.0
        self._connect()

    def _connect(self):
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((self.host, self.port))
            self._sock = sock
            self._device_id = self._raw_query("*IDN?")
            self.set_state(DevState.ON)
            self.set_status("Connected to detector.")
        except (OSError, CommunicationError) as e:
            self._sock = None
            self.set_state(DevState.FAULT)
            self.set_status(f"Cannot connect to {self.host}:{self.port}: {e}")
            self.error_stream(f"Connection failed: {e}")

    def _raw_query(self, command_str):
        try:
            self._sock.sendall((command_str + "\n").encode())
            response = self._sock.recv(1024)
        except (TimeoutError, ConnectionError, OSError, AttributeError) as e:
            raise CommunicationError(str(e)) from e
        if not response:
            raise CommunicationError("connection closed by remote end (empty read)")
        return response.decode(errors="replace").strip()

    def _query(self, command_str):
        if self.get_state() == DevState.FAULT:
            now = time.time()
            if now - self._last_reconnect_attempt < self.reconnect_interval:
                raise CommunicationError("not connected (reconnect on cooldown)")
            self._last_reconnect_attempt = now
            self._connect()
            if self.get_state() == DevState.FAULT:
                raise CommunicationError("reconnect attempt failed")

        try:
            result = self._raw_query(command_str)
        except CommunicationError as e:
            self.set_state(DevState.FAULT)
            self.set_status(f"Communication lost: {e}")
            self.error_stream(f"Communication error during query {command_str!r}: {e}")
            raise

        if self.get_state() != DevState.ON:
            self.set_state(DevState.ON)
            self.set_status("Recovered -- communication restored.")
        return result

    @attribute(dtype=str, label="Device ID")
    def DeviceID(self):
        return self._device_id

    @attribute(dtype=int, label="Frame Count")
    def FrameCount(self):
        raw = self._query("FRAME:COUNT?")
        try:
            return int(raw)
        except ValueError as e:
            self.set_state(DevState.ALARM)
            self.set_status(f"Malformed response from detector: {raw!r}")
            self.error_stream(f"Malformed FrameCount response: {raw!r}")
            raise MalformedResponseError(
                f"could not parse FrameCount from {raw!r}"
            ) from e

    @command(dtype_in=str, dtype_out=str)
    def Query(self, command_str):
        return self._query(command_str)


if __name__ == "__main__":
    run((TcpDetector,))

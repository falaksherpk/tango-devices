#!/usr/bin/env python3
"""
Lab 2.1 -- Tango device server wrapping an RS-232 instrument.

Talks to a real (or, here, socat-simulated) serial instrument that
responds to *IDN? -- same pattern used for any bench instrument on a
real RS-232 line.
"""
from tango import DevState
from tango.server import Device, attribute, command, device_property, run
import serial


class SerialInstrument(Device):

    port = device_property(dtype=str, default_value="/dev/pts/2")
    baudrate = device_property(dtype=int, default_value=9600)

    def init_device(self):
        Device.init_device(self)
        self._device_id = "UNKNOWN"
        try:
            self._ser = serial.Serial(self.port, baudrate=self.baudrate, timeout=2)
            self._device_id = self._query("*IDN?")
            self.set_state(DevState.ON)
        except Exception as e:
            self.error_stream(f"Failed to open serial port {self.port}: {e}")
            self.set_state(DevState.FAULT)

    def _query(self, command_str):
        """Send a command string, read one line back, return it stripped."""
        self._ser.write((command_str + "\r\n").encode())
        response = self._ser.readline().decode(errors="replace").strip()
        return response

    @attribute(dtype=str, label="Device ID")
    def DeviceID(self):
        return self._device_id

    @command(dtype_in=str, dtype_out=str)
    def Query(self, command_str):
        """Send an arbitrary command to the instrument, return its reply."""
        return self._query(command_str)


if __name__ == "__main__":
    run((SerialInstrument,))

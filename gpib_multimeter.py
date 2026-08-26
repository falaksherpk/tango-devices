#!/usr/bin/env python3
"""
Lab 2.4 -- Tango device server wrapping a GPIB instrument, simulated
via PyVISA's official @sim backend (pyvisa-sim) rather than real GPIB
bus hardware.

GPIB itself is a parallel-bus electrical/physical layer that can't be
faked in pure software the way a serial port (socat) or a raw TCP
socket can -- there's no equivalent virtual-bus trick. What CAN be
simulated is the layer real GPIB control code actually talks to in
practice: VISA (Virtual Instrument Software Architecture), via PyVISA.
Real GPIB-connected instruments in a lab are used through PyVISA
almost universally, whether over real GPIB hardware, a Prologix
GPIB-USB adapter, or (here) pyvisa-sim's simulated backend -- so this
device server's code is realistic and would need only a resource
address and VISA backend string change to talk to a real instrument.

Same abstraction pattern as every other lab in this chapter: the
transport/backend changes, the Tango-facing attribute/command shape
doesn't.
"""
from tango import DevState
from tango.server import Device, attribute, command, device_property, run
import pyvisa


class GpibMultimeter(Device):

    sim_yaml_path = device_property(
        dtype=str,
        default_value="/home/falak/tango-devices/protocols/gpib_sim.yaml",
    )
    resource_name = device_property(dtype=str, default_value="GPIB0::22::INSTR")

    def init_device(self):
        Device.init_device(self)
        self._device_id = "UNKNOWN"
        try:
            # '@sim' backend: simulates the VISA layer entirely in software.
            # For real hardware, this becomes '@py' (PyVISA-Py) or the
            # National Instruments VISA library, with resource_name pointing
            # at the real GPIB address instead -- see Real Hardware Notes.
            self._rm = pyvisa.ResourceManager(f"{self.sim_yaml_path}@sim")
            self._inst = self._rm.open_resource(
                self.resource_name, read_termination="\n", write_termination="\n"
            )
            self._device_id = self._inst.query("*IDN?")
            self.set_state(DevState.ON)
        except Exception as e:
            self.error_stream(f"Failed to open VISA resource {self.resource_name}: {e}")
            self.set_state(DevState.FAULT)

    @attribute(dtype=str, label="Device ID")
    def DeviceID(self):
        return self._device_id

    @attribute(dtype=float, label="Voltage", unit="V")
    def Voltage(self):
        # Polled fresh on every read -- a real measurement, not cached
        # config data, same distinction as Lab 2.2's FrameCount.
        return float(self._inst.query("MEAS:VOLT?"))

    @command(dtype_in=str, dtype_out=str)
    def Query(self, command_str):
        """Send an arbitrary SCPI command to the instrument, return its reply."""
        return self._inst.query(command_str)


if __name__ == "__main__":
    run((GpibMultimeter,))

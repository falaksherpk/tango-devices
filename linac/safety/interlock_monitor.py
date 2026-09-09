#!/usr/bin/env python3
"""
linac/safety/interlock1 (Chapter 6) -- Tango device server reading a
PLC-style Modbus TCP safety interlock. Same abstraction pattern as
every prior linac/ device: the transport is new, the Tango-facing
shape isn't.

Design decision, made before any code (see the real facility
precedent cited in fake_modbus_plc.py): this device is a supervisory
MONITOR of PLC-computed permit/fault status, not a device that itself
computes safety-critical trip logic. It reads coils; the one write it
performs (Reset) only requests an acknowledgement the PLC itself may
refuse (if a fault's cause still persists) -- it never asserts the
permit or the fault bits directly.

DevState carries real, distinct meaning here (first use of ALARM in
this project -- Ch2 only used attribute-level alarm *bounds*, never
device-state ALARM):
  ON     -- healthy comms, BeamPermit is True
  ALARM  -- healthy comms, BeamPermit is False (a genuine, correctly-
            reported safety condition -- not a malfunction)
  FAULT  -- comms to the PLC lost; per Ch5's Finding E/G precedent,
            refuses to serve stale attribute values while faulted,
            and self-heals automatically once comms return

Thread-safety and fault-handling design carried forward directly from
Ch5's VacuumGauge (proven, not reinvented): a single threading.Lock
around every Modbus call (the poll loop and the Reset command both
touch the client), a consecutive-failure counter before declaring
FAULT, and startup property validation built in from the start this
time rather than added by a later audit retrofit.
"""
import threading
import time

from tango import DevState
from tango.server import Device, attribute, command, device_property, run
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

ADDR_DOOR = 0
ADDR_VACUUM = 1
ADDR_WATER = 2
ADDR_PERMIT = 3
ADDR_FAULT = 4
ADDR_RESET = 5

POLL_FAILURE_THRESHOLD = 3


class InterlockMonitor(Device):

    Host = device_property(dtype=str, default_value="127.0.0.1")
    Port = device_property(dtype=int, default_value=5040)

    def init_device(self):
        Device.init_device(self)
        self._client_lock = threading.Lock()
        self._poll_failures = 0
        self._client = None
        self._stop_event = threading.Event()
        self._cache = {
            "door": False,
            "vacuum": False,
            "water": False,
            "permit": False,
            "fault": False,
        }

        if not 1 <= self.Port <= 65535:
            msg = f"Port must be 1-65535, got {self.Port}"
            self.error_stream(msg)
            self.set_state(DevState.FAULT)
            self.set_status(msg)
            return

        try:
            self._client = ModbusTcpClient(self.Host, port=self.Port)
            self._client.connect()
            if not self._client.connected:
                raise ConnectionError(f"could not connect to {self.Host}:{self.Port}")
        except (ConnectionError, OSError) as e:
            msg = f"Failed to connect to interlock PLC at {self.Host}:{self.Port}: {e}"
            self.error_stream(msg)
            self.set_state(DevState.FAULT)
            self.set_status(msg)
            return

        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _apply_state_from_cache(self):
        if self._cache["permit"]:
            self.set_state(DevState.ON)
            self.set_status("The device is in ON state.")
        else:
            self.set_state(DevState.ALARM)
            self.set_status(
                "Interlock tripped: "
                f"door_closed={self._cache['door']} "
                f"vacuum_ok={self._cache['vacuum']} "
                f"water_flow_ok={self._cache['water']} "
                f"fault_latched={self._cache['fault']}"
            )

    def _poll_loop(self):
        while not self._stop_event.is_set():
            with self._client_lock:
                try:
                    r = self._client.read_coils(address=0, count=6, device_id=1)
                    bits = r.bits[:6]
                except (ModbusException, OSError, AttributeError) as e:
                    self._poll_failures += 1
                    self.error_stream(
                        f"Poll failed ({self._poll_failures} consecutive): {e}"
                    )
                    if self._poll_failures >= POLL_FAILURE_THRESHOLD:
                        self.set_state(DevState.FAULT)
                        self.set_status(
                            f"Lost contact with interlock PLC at "
                            f"{self.Host}:{self.Port} after "
                            f"{self._poll_failures} consecutive failed polls. "
                            f"Last error: {e}"
                        )
                    time.sleep(1)
                    continue

            if self._poll_failures:
                self.error_stream(
                    f"Modbus contact restored after {self._poll_failures} "
                    f"failed polls"
                )
                self._poll_failures = 0

            self._cache = {
                "door": bits[ADDR_DOOR],
                "vacuum": bits[ADDR_VACUUM],
                "water": bits[ADDR_WATER],
                "permit": bits[ADDR_PERMIT],
                "fault": bits[ADDR_FAULT],
            }
            self._apply_state_from_cache()
            time.sleep(1)

    def _is_operational(self):
        return self.get_state() != DevState.FAULT

    @attribute(dtype=bool, label="Beam Permit")
    def BeamPermit(self):
        return self._cache["permit"]

    def is_BeamPermit_allowed(self, _req_type):
        return self._is_operational()

    @attribute(dtype=bool, label="Fault Latched")
    def FaultLatched(self):
        return self._cache["fault"]

    def is_FaultLatched_allowed(self, _req_type):
        return self._is_operational()

    @attribute(dtype=bool, label="Door Closed")
    def DoorClosed(self):
        return self._cache["door"]

    def is_DoorClosed_allowed(self, _req_type):
        return self._is_operational()

    @attribute(dtype=bool, label="Vacuum OK")
    def VacuumOk(self):
        return self._cache["vacuum"]

    def is_VacuumOk_allowed(self, _req_type):
        return self._is_operational()

    @attribute(dtype=bool, label="Water Flow OK")
    def WaterFlowOk(self):
        return self._cache["water"]

    def is_WaterFlowOk_allowed(self, _req_type):
        return self._is_operational()

    @command(dtype_out=str)
    def Reset(self):
        with self._client_lock:
            self._client.write_coil(address=ADDR_RESET, value=True, device_id=1)
        return "Reset requested"

    def is_Reset_allowed(self):
        return self._is_operational()

    def delete_device(self):
        self._stop_event.set()
        super().delete_device()


if __name__ == "__main__":
    run((InterlockMonitor,))

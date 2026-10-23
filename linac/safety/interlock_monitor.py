#!/usr/bin/env python3
"""
linac/safety/interlock1 (Chapter 6, rebuilt in Chapter 28) -- Tango
device server reading a PLC-style Modbus TCP safety interlock. Same
abstraction pattern as every prior linac/ device: the transport is new,
the Tango-facing shape isn't.

Design decision, made before any code (see the real facility
precedent cited in fake_modbus_plc.py): this device is a supervisory
MONITOR of PLC-computed permit/fault status, not a device that itself
computes safety-critical trip logic. It reads coils; the one write it
performs (Reset) only requests an acknowledgement the PLC itself may
refuse (if a fault's cause still persists) -- it never asserts the
permit or the fault bits directly.

DevState carries real, distinct meaning here:
  ON     -- healthy comms, BeamPermit is True
  ALARM  -- healthy comms, BeamPermit is False (a genuine, correctly-
            reported safety condition -- not a malfunction; the
            attributes are served and Reset is allowed)
  FAULT  -- no answer from the PLC (at startup, or 3 failed polls in a
            row); refuses to serve stale attribute values while
            faulted, and returns to ON or ALARM by itself once the PLC
            answers again -- also when it was started or Init-ed while
            the PLC was down (Ch28)

Thread-safety and lifetime as in Ch27's VacuumGauge: one threading.Lock
around every Modbus call (the poll loop and the Reset command both use
the client), a consecutive-failure counter before declaring FAULT, a
synchronous first read, a poll thread that runs whenever the properties
are valid, and a delete_device() that stops and joins it and closes the
connection. The device has no memorized attributes.
"""

import threading

import tango
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from tango import DevState
from tango.constants import StatusNotSet
from tango.server import Device, attribute, command, device_property, run

ADDR_DOOR = 0
ADDR_VACUUM = 1
ADDR_WATER = 2
ADDR_PERMIT = 3
ADDR_FAULT = 4
ADDR_RESET = 5
COIL_COUNT = 6
PLC_DEVICE_ID = 1

# Consecutive failed polls before the device declares itself FAULT.
POLL_FAILURE_THRESHOLD = 3

POLL_INTERVAL_S = 1.0
POLL_THREAD_NAME = "InterlockMonitor-poll"

# pymodbus's defaults (timeout=3, retries=3) let one call block for 12 s
# against a PLC that accepts and never answers (measured in Ch27 and again
# in Ch28), holding the lock all that time. One attempt of 1 s bounds every
# Modbus call; the device's own failure counter above is the retry policy.
MODBUS_TIMEOUT_S = 1.0
MODBUS_RETRIES = 0
# Longest a blocked poll can take to notice the stop event, plus margin.
THREAD_JOIN_TIMEOUT_S = MODBUS_TIMEOUT_S * (MODBUS_RETRIES + 1) + POLL_INTERVAL_S + 2


class InterlockMonitor(Device):
    Host = device_property(dtype=str, default_value="127.0.0.1")
    Port = device_property(dtype=int, default_value=5040)

    def init_device(self):
        Device.init_device(self)
        # Clear a status a failed init (or an earlier Init) left behind.
        self.set_status(StatusNotSet)
        self._client_lock = threading.Lock()
        self._poll_failures = 0
        self._cache = None
        self._client = None
        self._thread = None
        # Created before any early return, so delete_device() can always
        # use them, even if this device faults during startup.
        self._stop_event = threading.Event()

        if not 1 <= self.Port <= 65535:
            self._fault(f"Port must be 1-65535, got {self.Port}")
            return

        self._client = ModbusTcpClient(
            self.Host, port=self.Port, timeout=MODBUS_TIMEOUT_S, retries=MODBUS_RETRIES
        )

        # A synchronous first read: the device is ON or ALARM with real
        # coil values before init_device() returns, or FAULT with the
        # reason -- never UNKNOWN serving defaults (Ch6 Finding A).
        try:
            self._cache = self._read_coils()
            self._apply_state_from_cache()
        except (ModbusException, OSError, AttributeError) as e:
            self._drop_connection()
            self._fault(
                f"Failed to connect to interlock PLC at {self.Host}:{self.Port}: {e}"
            )

        # Started on both paths: a device that faulted at startup heals
        # by itself once the PLC answers.
        self._thread = threading.Thread(
            target=self._poll_loop, name=POLL_THREAD_NAME, daemon=True
        )
        self._thread.start()

    def _fault(self, msg):
        self.error_stream(msg)
        self.set_state(DevState.FAULT)
        self.set_status(msg)

    def _drop_connection(self):
        # After any failed call the connection can't be trusted: a timed-out
        # read may still get its late reply, and pymodbus 3.15 keeps the
        # socket after a timeout and after a plain OSError. Found live in
        # Ch28: a PLC hung, then its peer closed, and every poll failed with
        # EPIPE on that dead socket until Init. Closing here makes the next
        # call connect afresh.
        with self._client_lock:
            self._client.close()

    def _read_coils(self):
        with self._client_lock:
            result = self._client.read_coils(
                address=0, count=COIL_COUNT, device_id=PLC_DEVICE_ID
            )
        # A Modbus exception response has no .bits: AttributeError is
        # caught by the callers like a failed read.
        bits = result.bits
        return {
            "door": bits[ADDR_DOOR],
            "vacuum": bits[ADDR_VACUUM],
            "water": bits[ADDR_WATER],
            "permit": bits[ADDR_PERMIT],
            "fault": bits[ADDR_FAULT],
        }

    def _apply_state_from_cache(self):
        if self._cache["permit"]:
            self.set_state(DevState.ON)
            self.set_status(StatusNotSet)
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
        while not self._stop_event.wait(POLL_INTERVAL_S):
            try:
                cache = self._read_coils()
            except (ModbusException, OSError, AttributeError) as e:
                if self._stop_event.is_set():
                    return
                self._drop_connection()
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
                continue

            if self._stop_event.is_set():
                return

            # A good read clears the failure streak; a device that was
            # FAULT (at startup or later) returns to whichever of ON or
            # ALARM the PLC reports. Recovery is good news: INFO, not ERROR.
            if self._poll_failures or self.get_state() == DevState.FAULT:
                after = (
                    f"{self._poll_failures} failed polls"
                    if self._poll_failures
                    else "a failed first read"
                )
                self.info_stream(f"Contact with interlock PLC restored after {after}")
                self._poll_failures = 0

            self._cache = cache
            self._apply_state_from_cache()

    def _is_operational(self):
        # ALARM is a healthy state here (comms fine, interlock tripped):
        # the coils are served and Reset must be allowed in it.
        return self.get_state() in (DevState.ON, DevState.ALARM)

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
        """Request a reset of the latched fault. The PLC clears the latch
        only if every input is safe; the result shows in FaultLatched and
        the state at the next poll (within POLL_INTERVAL_S)."""
        try:
            with self._client_lock:
                result = self._client.write_coil(
                    address=ADDR_RESET, value=True, device_id=PLC_DEVICE_ID
                )
        except (ModbusException, OSError) as e:
            self._drop_connection()
            tango.Except.throw_exception(
                "InterlockMonitor_ResetNotDelivered",
                f"Reset not delivered to the interlock PLC at "
                f"{self.Host}:{self.Port}: {e}",
                "InterlockMonitor.Reset",
            )
        if result.isError():
            tango.Except.throw_exception(
                "InterlockMonitor_ResetRefused",
                f"The interlock PLC refused the reset write: {result}",
                "InterlockMonitor.Reset",
            )
        return "Reset requested"

    def is_Reset_allowed(self):
        return self._is_operational()

    def delete_device(self):
        # Init and server shutdown call this: stop the poll thread first,
        # so it can't touch a deleted device, then close the connection
        # so Init doesn't leak one.
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=THREAD_JOIN_TIMEOUT_S)
            if self._thread.is_alive():
                self.error_stream("poll thread did not stop within its timeout")
            self._thread = None
        if self._client is not None:
            with self._client_lock:
                self._client.close()
            self._client = None
        super().delete_device()


if __name__ == "__main__":
    run((InterlockMonitor,))

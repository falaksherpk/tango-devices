#!/usr/bin/env python3
"""
linac/vacuum/gauge1 -- Tango device server wrapping a Modbus TCP vacuum
gauge. Same abstraction pattern as every other linac/ device: the
transport changes, the Tango-facing shape doesn't.

Evolved from the pre-Part-3 exploratory series' ModbusVacuumController
(Phases 2-3): the register map (pressure + pump status), the pymodbus
SimData/SimDevice-based fake instrument, and the threading.Lock /
cached-attribute design carry forward.

A background poll thread reads VacuumPressure once a second and fires a
Tango change event on a meaningful change.

Thread safety, carried forward from Phase 3: pymodbus's docs say
ModbusTcpClient is not thread safe and that the application must
serialize calls. The poll thread and Tango's request thread (PumpStatus,
PumpOn, PumpOff) both use self._client, so a single threading.Lock
guards every Modbus call, and VacuumPressure is served from the poll
thread's cache instead of a Modbus round trip per read.

Faults (Part 3 Ch5 LAB 5.6, reworked in Ch27): after
POLL_FAILURE_THRESHOLD consecutive failed polls the device goes to FAULT
and refuses to serve its cached pressure; the next good read brings it
back to ON. The poll thread always runs once the properties are valid,
so this also holds for a device started, or Init-ed, while the
instrument is down. pymodbus's client connects on the first call after
a failure, so there is no hand-written reconnect logic here.

Lifetime (Ch27): delete_device() stops and joins the poll thread and
closes the Modbus connection. Init and server shutdown both call it, so
Init leaves exactly one poll thread and one connection.

PumpStatus is memorized (hw_memorized). When the instrument answers at
startup, Tango writes the last value back, so a restart restarts the
pump. When it doesn't, the device is in FAULT and Tango refuses the
restore, and remembers that (observed live in Ch27): once the device
has recovered, clients see ALARM with "Memorized attribute PumpStatus
has failed during device startup sequence", and reads of PumpStatus
return the refused write's error, while VacuumPressure is served. A
write of PumpStatus clears it (False keeps the pump off); Init with the
instrument up runs the restore again, which starts the pump if the
stored value is true. Only writes of the attribute are memorized, not
the PumpOn/PumpOff commands.
"""
import threading

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from tango import AttrWriteType, DevState
from tango.constants import StatusNotSet
from tango.server import Device, attribute, command, device_property, run

# How much VacuumPressure must change (in mbar) before a change event is
# pushed. Without it the poll loop would push an event every second even
# when the value is flat (pump off), which is the event flooding a real
# control-room system is designed to avoid.
PRESSURE_CHANGE_THRESHOLD = 0.001  # mbar

# Consecutive failed polls before the device declares itself FAULT.
# Short enough to be honest quickly, long enough that a single
# transient blip doesn't flap the state.
POLL_FAILURE_THRESHOLD = 3

POLL_INTERVAL_S = 1.0
POLL_THREAD_NAME = "VacuumGauge-poll"

# pymodbus's defaults (timeout=3, retries=3) let one read block for 12 s
# against an instrument that accepts and never answers (measured in Ch27),
# holding the lock all that time. One attempt of 1 s bounds every Modbus
# call; the device's own failure counter above is the retry policy.
MODBUS_TIMEOUT_S = 1.0
MODBUS_RETRIES = 0
# Longest a blocked poll can take to notice the stop event, plus margin.
THREAD_JOIN_TIMEOUT_S = MODBUS_TIMEOUT_S * (MODBUS_RETRIES + 1) + POLL_INTERVAL_S + 2

# Modbus unit/device IDs are 0-247 (247 is the last valid slave address
# in the specification; 248-255 are reserved).
MAX_MODBUS_DEVICE_ID = 247


class VacuumGauge(Device):

    Host = device_property(dtype=str, default_value="127.0.0.1")
    Port = device_property(dtype=int, default_value=5020)
    ModbusDeviceId = device_property(dtype=int, default_value=1)

    def init_device(self):
        Device.init_device(self)
        # Clear a status a failed init (or an earlier Init) left behind;
        # with no explicit status, Tango reports "The device is in <state>
        # state."
        self.set_status(StatusNotSet)
        self._client_lock = threading.Lock()
        self._pressure = None
        self._last_pushed_pressure = None
        self._poll_failures = 0
        self._client = None
        self._thread = None
        # Created before any early return, so delete_device() can always
        # use them, even if this device faults during startup.
        self._stop_event = threading.Event()

        if not 1 <= self.Port <= 65535:
            self._fault(f"Port must be 1-65535, got {self.Port}")
            return

        if not 0 <= self.ModbusDeviceId <= MAX_MODBUS_DEVICE_ID:
            self._fault(
                f"ModbusDeviceId must be 0-{MAX_MODBUS_DEVICE_ID}, "
                f"got {self.ModbusDeviceId}"
            )
            return

        self.set_change_event("VacuumPressure", True, False)
        self._client = ModbusTcpClient(
            self.Host, port=self.Port, timeout=MODBUS_TIMEOUT_S, retries=MODBUS_RETRIES
        )

        # A synchronous first read: the device is ON with a real pressure
        # before init_device() returns (and before Tango restores the
        # memorized PumpStatus), or FAULT with the reason.
        try:
            self._pressure = self._read_pressure()
            self.set_state(DevState.ON)
        except (ModbusException, OSError, AttributeError) as e:
            self._drop_connection()
            self._fault(
                f"Failed to connect to Modbus device at {self.Host}:{self.Port}: {e}"
            )

        # Started on both paths: a device that faulted at startup heals
        # by itself once the instrument answers.
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
        # Ch28 (interlock, same client pattern): an instrument hung, then its
        # peer closed, and every poll failed with EPIPE on that dead socket
        # until Init. Closing here makes the next call connect afresh.
        with self._client_lock:
            self._client.close()

    def _call(self, fn, **kwargs):
        """One Modbus call under the lock; on failure, drop the connection
        and re-raise (Tango reports it to the caller as DevFailed)."""
        try:
            with self._client_lock:
                return fn(device_id=self.ModbusDeviceId, **kwargs)
        except (ModbusException, OSError):
            self._drop_connection()
            raise

    def _read_pressure(self):
        with self._client_lock:
            result = self._client.read_holding_registers(
                address=0, count=1, device_id=self.ModbusDeviceId
            )
        # A Modbus exception response has no .registers: AttributeError
        # is caught by the callers like a failed read.
        return result.registers[0] / 1000.0

    def _poll_loop(self):
        while not self._stop_event.wait(POLL_INTERVAL_S):
            try:
                pressure = self._read_pressure()
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
                        f"Lost contact with Modbus device at "
                        f"{self.Host}:{self.Port} after {self._poll_failures} "
                        f"consecutive failed polls. Last error: {e}"
                    )
                continue

            if self._stop_event.is_set():
                return

            # A good read clears the failure streak and, if the device
            # was faulted (at startup or later), brings it back to ON.
            if self._poll_failures or self.get_state() != DevState.ON:
                self.error_stream(
                    f"Modbus contact restored after {self._poll_failures} "
                    f"failed polls"
                )
                self._poll_failures = 0
                self.set_state(DevState.ON)
                self.set_status(StatusNotSet)

            self._pressure = pressure

            if (
                self._last_pushed_pressure is None
                or abs(pressure - self._last_pushed_pressure)
                >= PRESSURE_CHANGE_THRESHOLD
            ):
                self.push_change_event("VacuumPressure", pressure)
                self._last_pushed_pressure = pressure

    def _is_operational(self):
        return self.get_state() == DevState.ON

    @attribute(dtype=float, label="Vacuum Pressure", unit="mbar")
    def VacuumPressure(self):
        # The poll thread's cached value: no blocking Modbus round trip
        # per Tango read, and no second thread touching self._client.
        return self._pressure

    def is_VacuumPressure_allowed(self, _req_type):
        # Refuse to serve the cached reading unless ON: a stale pressure
        # presented as current is worse than no reading at all for
        # anything downstream making decisions on it.
        return self._is_operational()

    @attribute(
        dtype=bool,
        label="Pump Status",
        access=AttrWriteType.READ_WRITE,
        memorized=True,
        hw_memorized=True,
    )
    def PumpStatus(self):
        result = self._call(self._client.read_holding_registers, address=1, count=1)
        return bool(result.registers[0])

    @PumpStatus.setter
    def PumpStatus(self, value):
        self._call(self._client.write_register, address=1, value=int(value))

    def is_PumpStatus_allowed(self, _req_type):
        return self._is_operational()

    @command(dtype_out=str)
    def PumpOn(self):
        self._call(self._client.write_register, address=1, value=1)
        return "Pump turned ON"

    def is_PumpOn_allowed(self):
        return self._is_operational()

    @command(dtype_out=str)
    def PumpOff(self):
        self._call(self._client.write_register, address=1, value=0)
        return "Pump turned OFF"

    def is_PumpOff_allowed(self):
        return self._is_operational()

    def delete_device(self):
        # Init and server shutdown call this: stop the poll thread first,
        # so it can't push events for a deleted device, then close the
        # connection so Init doesn't leak one.
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
    run((VacuumGauge,))

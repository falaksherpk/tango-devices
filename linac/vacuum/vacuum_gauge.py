#!/usr/bin/env python3
"""
linac/vacuum/gauge1 (Chapter 5) -- Tango device server wrapping a
Modbus TCP vacuum gauge. Same abstraction pattern as every prior
linac/ device -- the transport changes, the Tango-facing shape
doesn't.

Evolved from the pre-Part-3 exploratory series' ModbusVacuumController
(Phases 2-3): the original register map (pressure + pump status),
pymodbus SimData/SimDevice-based fake instrument, and the Phase 3
threading.Lock/cached-attribute design all carry forward unchanged --
each was a real, live-debugged decision, not just a generic starting
point.

A background polling thread (same pattern as TemperatureSensor,
Phase 1 Lab 1.2, and this project's other polling devices) watches
VacuumPressure and, on a meaningful change, both (a) fires a real
Tango change_event and (b) pushes the reading into a Redis Stream.

Thread-safety note, carried forward from Phase 3: pymodbus's own docs
are explicit that ModbusTcpClient is "NOT thread safe... the
application must ensure that calls are serialized." Since this device
has TWO things that can touch self._client -- the background poller,
and the on-demand PumpStatus setter / PumpOn / PumpOff commands, both
callable from Tango's own request thread at any moment -- a single
threading.Lock guards every Modbus call, and VacuumPressure reads from
a thread-updated cache rather than hitting Modbus directly on every
attribute access.

Fault handling (Chapter 5 LAB 5.6, added after real live testing):
killing the instrument mid-run was tested rather than assumed, and
showed the carried-forward design had a genuine gap -- the device
stayed ON indefinitely while serving a stale cached pressure, and
on-demand calls blocked behind the poll thread's held lock until
CORBA timed out. It DID recover by itself once the instrument
returned (pymodbus's client reconnects internally), so no hand-written
reconnect logic is needed here -- a real difference from Ch4's
FaradayCup, which needed one only because it drives a raw asyncio
socket with no library doing that job. What was missing was honesty
about state: this version faults after POLL_FAILURE_THRESHOLD
consecutive failures and refuses to serve stale data while faulted,
rather than silently reporting a healthy device with a frozen reading.
"""
import threading
import time

import redis
from tango import AttrWriteType, DevState
from tango.server import Device, attribute, command, device_property, run
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

# How much VacuumPressure must change (in mbar) before we bother firing
# a change_event / Redis push. Without this, the background poll loop
# would push an event every single second even when the value is
# effectively unchanged (e.g. pump off, pressure flat) -- exactly the
# kind of event-flooding a real control-room system is designed to
# avoid.
PRESSURE_CHANGE_THRESHOLD = 0.001  # mbar

# Consecutive failed polls before the device declares itself FAULT.
# Short enough to be honest quickly, long enough that a single
# transient blip doesn't flap the state.
POLL_FAILURE_THRESHOLD = 3

REDIS_STREAM_KEY = "vacuum:pressure"

# Modbus unit/device IDs are 0-247 (247 is the last valid slave address
# in the specification; 248-255 are reserved).
MAX_MODBUS_DEVICE_ID = 247


class VacuumGauge(Device):

    Host = device_property(dtype=str, default_value="127.0.0.1")
    Port = device_property(dtype=int, default_value=5020)
    ModbusDeviceId = device_property(dtype=int, default_value=1)

    def init_device(self):
        Device.init_device(self)
        self._client_lock = threading.Lock()
        self._pressure = 0.0
        self._last_pushed_pressure = None
        self._poll_failures = 0
        self._client = None
        # Created before any early return so delete_device() can always
        # set it, even if this device faults during startup.
        self._stop_event = threading.Event()

        if not 1 <= self.Port <= 65535:
            msg = f"Port must be 1-65535, got {self.Port}"
            self.error_stream(msg)
            self.set_state(DevState.FAULT)
            self.set_status(msg)
            return

        if not 0 <= self.ModbusDeviceId <= MAX_MODBUS_DEVICE_ID:
            msg = (
                f"ModbusDeviceId must be 0-{MAX_MODBUS_DEVICE_ID}, "
                f"got {self.ModbusDeviceId}"
            )
            self.error_stream(msg)
            self.set_state(DevState.FAULT)
            self.set_status(msg)
            return

        try:
            self._client = ModbusTcpClient(self.Host, port=self.Port)
            self._client.connect()
            if not self._client.connected:
                raise ConnectionError(f"could not connect to {self.Host}:{self.Port}")
            self.set_state(DevState.ON)
        except (ConnectionError, OSError) as e:
            msg = f"Failed to connect to Modbus device at {self.Host}:{self.Port}: {e}"
            self.error_stream(msg)
            self.set_state(DevState.FAULT)
            self.set_status(msg)
            return

        self.set_change_event("VacuumPressure", True, False)

        self._redis = redis.Redis(
            host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=10
        )

        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        while not self._stop_event.is_set():
            with self._client_lock:
                try:
                    result = self._client.read_holding_registers(
                        address=0, count=1, device_id=self.ModbusDeviceId
                    )
                    pressure = result.registers[0] / 1000.0
                # AttributeError guards against a malformed/error Modbus
                # response (missing .registers) since isError() isn't checked above.
                except (ModbusException, OSError, AttributeError) as e:
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
                    time.sleep(1)
                    continue

            # A good read clears the failure streak and, if we had
            # faulted, brings the device back to ON by itself --
            # pymodbus's client reconnects internally, so recovery
            # needs no hand-written reconnect logic here.
            if self._poll_failures:
                self.error_stream(
                    f"Modbus contact restored after {self._poll_failures} "
                    f"failed polls"
                )
                self._poll_failures = 0
                self.set_state(DevState.ON)
                self.set_status("The device is in ON state.")

            self._pressure = pressure

            if (
                self._last_pushed_pressure is None
                or abs(pressure - self._last_pushed_pressure)
                >= PRESSURE_CHANGE_THRESHOLD
            ):
                self.push_change_event("VacuumPressure", pressure)
                try:
                    self._redis.xadd(REDIS_STREAM_KEY, {"pressure": f"{pressure:.4f}"})
                except redis.exceptions.RedisError as e:
                    self.error_stream(f"Redis push failed: {e}")
                self._last_pushed_pressure = pressure

            time.sleep(1)

    def _is_operational(self):
        return self.get_state() != DevState.FAULT

    @attribute(dtype=float, label="Vacuum Pressure", unit="mbar")
    def VacuumPressure(self):
        # Returns the background thread's cached value -- no blocking
        # Modbus round-trip on every Tango attribute read, and avoids
        # a second thread touching self._client outside the lock.
        return self._pressure

    def is_VacuumPressure_allowed(self, _req_type):
        # Refuse to serve the cached reading while faulted: a stale
        # pressure presented as current is worse than no reading at all
        # for anything downstream making decisions on it.
        return self._is_operational()

    @attribute(
        dtype=bool,
        label="Pump Status",
        access=AttrWriteType.READ_WRITE,
        memorized=True,
        hw_memorized=True,
    )
    def PumpStatus(self):
        with self._client_lock:
            result = self._client.read_holding_registers(
                address=1, count=1, device_id=self.ModbusDeviceId
            )
            return bool(result.registers[0])

    @PumpStatus.setter
    def PumpStatus(self, value):
        with self._client_lock:
            self._client.write_register(
                address=1, value=int(value), device_id=self.ModbusDeviceId
            )

    def is_PumpStatus_allowed(self, _req_type):
        return self._is_operational()

    @command(dtype_out=str)
    def PumpOn(self):
        with self._client_lock:
            self._client.write_register(
                address=1, value=1, device_id=self.ModbusDeviceId
            )
        return "Pump turned ON"

    def is_PumpOn_allowed(self):
        return self._is_operational()

    @command(dtype_out=str)
    def PumpOff(self):
        with self._client_lock:
            self._client.write_register(
                address=1, value=0, device_id=self.ModbusDeviceId
            )
        return "Pump turned OFF"

    def is_PumpOff_allowed(self):
        return self._is_operational()

    def delete_device(self):
        self._stop_event.set()
        self._thread.join(timeout=2)
        super().delete_device()


if __name__ == "__main__":
    run((VacuumGauge,))

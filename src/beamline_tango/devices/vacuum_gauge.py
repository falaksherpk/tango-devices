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

PumpStatus is memorized but not hw_memorized (Part 4 Ch38, owner's
decision): the device never switches the pump by itself, at startup,
after Init, or when the instrument comes back after a restart. The last
command (a write of PumpStatus, PumpOn or PumpOff) is kept as the
memorized value in the database, and every poll reads the pump's actual
state with the pressure. When the two differ, State is ALARM and Status
says which way and what to do; it clears when they agree again. Tango's
own write value can't serve as the last command: a never-written
PumpStatus reads True, so the memorized property is read instead, and a
device that never had a command checks nothing. Until Ch38 PumpStatus was
hw_memorized, so a restart started the pump, while an instrument restart
left it off with nothing reported.

Recovery (Ch38): a good poll after failed ones resets the count, but
only a device in FAULT goes back to ON. After one or two failed polls
the device never left its state, and setting ON there overwrote an ALARM
from a pressure past its level.

State and the pressure levels (Ch41, Ch39 Case 6): each meaningful
change, the poll thread pushes a change event with the bare value, which
leaves VacuumPressure's stored quality ATTR_VALID. When the attribute is
polled, as HDB++ polls it on the fleet, cppTango's default dev_state()
uses that stored quality instead of checking the levels, so State read
ON while the pressure was past them. dev_state() now compares the cached
pressure with the configured levels itself, with Tango's own rule: a
value at a level counts as past it, and a warning level makes State
ALARM too. The events themselves carry the right quality.

Poll age (Part 4 Ch42, Ch39 Case 2): State can't show a poll thread that
stopped, since the device keeps its last state. PollAge is the time since
the last good read of the instrument (or since the device started, before
the first one), readable in every state, FAULT included; it grows
whenever polling stops, for any reason.
"""

import operator
import threading
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from tango import AttrWriteType, DevFailed, DevState, Except, Util
from tango.constants import AlrmValueNotSpec, StatusNotSet
from tango.server import Device, attribute, command, device_property, run

# VacuumPressure's levels, alarms first, each with Tango's own test of a
# value past it (measured with cppTango 10.3.3, Ch41: a value at the level
# counts). The site sets the levels in the Tango database (tango_ds role).
PRESSURE_LEVELS = (
    ("min_alarm", operator.le),
    ("max_alarm", operator.ge),
    ("min_warning", operator.le),
    ("max_warning", operator.ge),
)

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
        self._pump = None  # the pump's actual state, from the last poll
        self._pump_command = self._load_pump_command()
        self._reported_pump_mismatch = ""
        self._last_pushed_pressure = None
        self._poll_failures = 0
        # time.monotonic() of the last good read; the start until the first.
        self._last_good_read = time.monotonic()
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
            self._pressure, self._pump = self._read_instrument()
            self._last_good_read = time.monotonic()
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
                result = fn(device_id=self.ModbusDeviceId, **kwargs)
        except (ModbusException, OSError):
            self._drop_connection()
            raise
        # An exception response is a refusal, not a result: a refused pump
        # write must not count as done (Ch38).
        if result.isError():
            raise ModbusException(f"exception response from the gauge: {result}")
        return result

    def _read_instrument(self):
        """Pressure (mbar) and pump state, in one Modbus call."""
        with self._client_lock:
            result = self._client.read_holding_registers(
                address=0, count=2, device_id=self.ModbusDeviceId
            )
        # A Modbus exception response is a failed read. Its .registers is an
        # empty list in pymodbus 3.15, not missing: indexing it raised
        # IndexError, which no caller caught, so the gauge couldn't start or
        # its poll thread died (Ch38). ModbusException is caught like any
        # failed read.
        if result.isError():
            raise ModbusException(f"exception response from the gauge: {result}")
        return result.registers[0] / 1000.0, bool(result.registers[1])

    def _poll_loop(self):
        while not self._stop_event.wait(POLL_INTERVAL_S):
            try:
                pressure, pump = self._read_instrument()
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

            # A good read clears the failure streak; only a device in FAULT
            # (at startup or later) goes back to ON. Below the threshold the
            # device never left its state, which may be ALARM from a pressure
            # past its level: setting ON there overwrote it (Ch38, item 4).
            # Recovery is good news: INFO, not ERROR (as the interlock, Ch28).
            faulted = self.get_state() == DevState.FAULT
            if self._poll_failures or faulted:
                after = (
                    f"{self._poll_failures} failed polls"
                    if self._poll_failures
                    else "a failed first read"
                )
                self.info_stream(f"Modbus contact restored after {after}")
                self._poll_failures = 0
            if faulted:
                self.set_state(DevState.ON)
                self.set_status(StatusNotSet)

            self._pressure = pressure
            self._pump = pump
            self._last_good_read = time.monotonic()
            self._log_pump_mismatch()

            if (
                self._last_pushed_pressure is None
                or abs(pressure - self._last_pushed_pressure)
                >= PRESSURE_CHANGE_THRESHOLD
            ):
                self.push_change_event("VacuumPressure", pressure)
                self._last_pushed_pressure = pressure

    # --- The pump against its last command (Ch38, item 5) -----------------

    def _load_pump_command(self):
        """The last pump command: the memorized value in the database, or
        None if there has never been one."""
        try:
            prop = (
                Util.instance()
                .get_database()
                .get_device_attribute_property(self.get_name(), ["PumpStatus"])[
                    "PumpStatus"
                ]
            )
        except DevFailed as exc:
            self.warn_stream(f"Last pump command unknown, not checked: {exc}")
            return None
        value = prop.get("__value")
        return value[0].lower() == "true" if value else None

    def _commanded(self, value):
        # Called after the register write succeeded: the pump is as commanded
        # now, so there is nothing to report until the next poll says otherwise.
        self._pump_command = bool(value)
        self._pump = bool(value)
        self._log_pump_mismatch()

    def _pump_mismatch(self):
        command, actual = self._pump_command, self._pump
        if command is None or actual is None or command == actual:
            return ""
        if command:
            return (
                "Pump is off, but its last command was ON (did the gauge "
                "restart?). PumpOn restarts it; PumpOff accepts it off."
            )
        return (
            "Pump is on, but its last command was OFF (switched on at the "
            "controller?). PumpOff stops it; PumpOn accepts it on."
        )

    def _log_pump_mismatch(self):
        mismatch = self._pump_mismatch()
        if mismatch == self._reported_pump_mismatch:
            return
        if mismatch:
            self.warn_stream(mismatch)
        else:
            self.info_stream("Pump agrees with its last command again")
        self._reported_pump_mismatch = mismatch

    def _pressure_past_level(self):
        """The first level the cached pressure is at or past, as (name,
        level, pressure), or None. Levels not set are skipped; Tango accepts only
        numbers for a level, so a set one always parses."""
        pressure = self._pressure
        if pressure is None:
            return None
        props = (
            self.get_device_attr().get_attr_by_name("VacuumPressure").get_properties()
        )
        for name, is_past in PRESSURE_LEVELS:
            text = getattr(props, name)
            if text != AlrmValueNotSpec and is_past(pressure, float(text)):
                return name, float(text), pressure
        return None

    def dev_state(self):
        # Tango's own evaluation (FAULT as set, ALARM for an attribute past
        # its level), plus the pump and the pressure against its levels,
        # which Tango misses when VacuumPressure is polled (Ch41). Nothing is
        # stored: when both are fine again, the next State read is ON with
        # no state to undo.
        state = super().dev_state()
        if state in (DevState.ON, DevState.ALARM) and (
            self._pump_mismatch() or self._pressure_past_level()
        ):
            return DevState.ALARM
        return state

    def dev_status(self):
        if self.get_state() != DevState.FAULT:
            mismatch = self._pump_mismatch()
            if mismatch:
                return mismatch
            past = self._pressure_past_level()
            if past:
                name, level, pressure = past
                return (
                    f"VacuumPressure {pressure:.2e} mbar is at or past "
                    f"its {name.replace('_', ' ')} level ({level:.2e} mbar)."
                )
        return super().dev_status()

    def _is_operational(self):
        # ALARM included, as in the interlock (Ch28): with VacuumPressure past an
        # alarm level, Tango keeps the device in ALARM once a client reads State,
        # and refusing there would refuse PumpOn, the action that clears it
        # (Ch32, 31 Oct). FAULT, no contact with the gauge, stays refused.
        return self.get_state() in (DevState.ON, DevState.ALARM)

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

    @attribute(dtype=float, label="Poll Age", unit="s", format="%.1f")
    def PollAge(self):
        # No is_PollAge_allowed: it must be readable in FAULT, where it
        # shows how long the gauge has been out of contact.
        return time.monotonic() - self._last_good_read

    @attribute(
        dtype=bool,
        label="Pump Status",
        access=AttrWriteType.READ_WRITE,
        memorized=True,
    )
    def PumpStatus(self):
        result = self._call(self._client.read_holding_registers, address=1, count=1)
        return bool(result.registers[0])

    @PumpStatus.setter
    def PumpStatus(self, value):
        self._call(self._client.write_register, address=1, value=int(value))
        self._commanded(value)

    def is_PumpStatus_allowed(self, _req_type):
        return self._is_operational()

    @command(dtype_out=str)
    def PumpOn(self):
        self._call(self._client.write_register, address=1, value=1)
        self._commanded(True)
        self._memorize_pump_status(True)
        return "Pump turned ON"

    def is_PumpOn_allowed(self):
        return self._is_operational()

    @command(dtype_out=str)
    def PumpOff(self):
        self._call(self._client.write_register, address=1, value=0)
        self._commanded(False)
        self._memorize_pump_status(False)
        return "Pump turned OFF"

    def is_PumpOff_allowed(self):
        return self._is_operational()

    def _memorize_pump_status(self, value):
        """Make a pump command survive a restart, as a write of PumpStatus does.

        Tango memorizes only attribute writes: the write value, and the
        "__value" property in the database that hw_memorized restores after
        init_device(). Called after the register write, so a failure here
        leaves the pump as commanded and tells the caller what a restart
        would do (Ch32, 31 Oct; the same fix as the magnet's Reset).
        """
        self.get_device_attr().get_w_attr_by_name("PumpStatus").set_write_value(value)
        try:
            Util.instance().get_database().put_device_attribute_property(
                self.get_name(), {"PumpStatus": {"__value": [str(value).lower()]}}
            )
        except DevFailed as exc:
            state = "ON" if value else "OFF"
            Except.re_throw_exception(
                exc,
                "VacuumGauge_PumpStatusNotMemorized",
                f"Pump turned {state}, but that could not be memorized: a restart "
                "would restore the last written PumpStatus.",
                "VacuumGauge._memorize_pump_status",
            )

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

#!/usr/bin/env python3
"""
Lab 2.3 / 3.3 -- Tango device server wrapping a Modbus TCP vacuum
controller. Same abstraction pattern as Labs 2.1/2.2 -- transport
changes, the Tango-facing shape doesn't.

Lab 3.3 addition: a background polling thread (same pattern as
TemperatureSensor, Lab 3.1) watches VacuumPressure and, on a
meaningful change, both (a) fires a real Tango change_event and
(b) pushes the reading into a Redis Stream -- mirroring this
project's real HDB++ archiving experience with a modern streaming
backend alongside the live event path.

Thread-safety note: pymodbus's own docs are explicit that
ModbusTcpClient is "NOT thread safe... the application must ensure
that calls are serialized." Since this device now has TWO things that
can touch self._client -- the new background poller, and the existing
on-demand PumpStatus setter / PumpOn / PumpOff commands, both callable
from Tango's own request thread at any moment -- a single
threading.Lock now guards every Modbus call, and VacuumPressure reads
from a thread-updated cache rather than hitting Modbus directly on
every attribute access (avoiding both the thread-safety issue and a
blocking round-trip on every read).
"""
import threading
import time

import redis
from tango import DevState
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

REDIS_STREAM_KEY = "vacuum:pressure"


class ModbusVacuumController(Device):

    host = device_property(dtype=str, default_value="127.0.0.1")
    port = device_property(dtype=int, default_value=5020)
    modbus_device_id = device_property(dtype=int, default_value=1)

    def init_device(self):
        Device.init_device(self)
        self._client_lock = threading.Lock()
        self._pressure = 0.0
        self._last_pushed_pressure = None

        try:
            self._client = ModbusTcpClient(self.host, port=self.port)
            self._client.connect()
            if not self._client.connected:
                raise ConnectionError(f"could not connect to {self.host}:{self.port}")
            self.set_state(DevState.ON)
        except (ConnectionError, OSError) as e:
            self.error_stream(
                f"Failed to connect to Modbus device at {self.host}:{self.port}: {e}"
            )
            self.set_state(DevState.FAULT)
            return

        self.set_change_event("VacuumPressure", True, False)

        self._redis = redis.Redis(
            host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=10
        )

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        while not self._stop_event.is_set():
            with self._client_lock:
                try:
                    result = self._client.read_holding_registers(
                        address=0, count=1, device_id=self.modbus_device_id
                    )
                    pressure = result.registers[0] / 1000.0
                # AttributeError guards against a malformed/error Modbus
                # response (missing .registers) since isError() isn't checked above.
                except (ModbusException, OSError, AttributeError) as e:
                    self.error_stream(f"Poll failed: {e}")
                    time.sleep(1)
                    continue

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

    @attribute(dtype=float, label="Vacuum Pressure", unit="mbar")
    def VacuumPressure(self):
        # Returns the background thread's cached value -- no blocking
        # Modbus round-trip on every Tango attribute read, and avoids
        # a second thread touching self._client outside the lock.
        return self._pressure

    @attribute(dtype=bool, label="Pump Status")
    def PumpStatus(self):
        with self._client_lock:
            result = self._client.read_holding_registers(
                address=1, count=1, device_id=self.modbus_device_id
            )
            return bool(result.registers[0])

    @PumpStatus.setter
    def PumpStatus(self, value):
        with self._client_lock:
            self._client.write_register(
                address=1, value=int(value), device_id=self.modbus_device_id
            )

    @command(dtype_out=str)
    def PumpOn(self):
        with self._client_lock:
            self._client.write_register(
                address=1, value=1, device_id=self.modbus_device_id
            )
        return "Pump turned ON"

    @command(dtype_out=str)
    def PumpOff(self):
        with self._client_lock:
            self._client.write_register(
                address=1, value=0, device_id=self.modbus_device_id
            )
        return "Pump turned OFF"

    def delete_device(self):
        self._stop_event.set()
        super().delete_device()


if __name__ == "__main__":
    run((ModbusVacuumController,))

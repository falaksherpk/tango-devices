#!/usr/bin/env python3
"""
Fake Modbus TCP vacuum gauge for linac/vacuum/gauge1 (Chapter 5) --
simulates a PLC-style device with holding registers, same
register-mapping mental model as a real Siemens PLC, just over Modbus
instead of PROFIBUS/S7 protocol.

Evolved unchanged from the pre-Part-3 exploratory series' Lab 2.3
(Phases 2-3) -- built on pymodbus 3.15's current SimData/SimDevice
architecture (the older ModbusSlaveContext/getValues/setValues API is
deprecated and being removed in pymodbus 4.0). Live register behavior
is implemented entirely inside the `action` callback, which pymodbus
invokes on every register access -- no background thread needed.

Register map (protocol addresses, 0-indexed):
  0: VacuumPressure -- scaled integer, real pressure (mbar x 1000)
  1: PumpStatus      -- 0 = off, 1 = on

Real issues found building this:
1. pymodbus invokes `action` more than once per write request -- at
   least once with real set_values, and again with set_values=None
   (an internal re-check/readback pass). Must guard with
   `set_values is not None` before indexing into it.
2. Mutating current_registers[0] in the callback DOES persist back to
   the live register (it's a reference into actual storage, not a
   copy) -- so the decay computation must capture pressure_at_pump_on
   from the *actual current register value* the moment the pump turns
   on, not decay from a hardcoded baseline every time. The first
   version of this script always decayed from a constant 1000,
   which made pressure jump back UP toward that curve if the pump
   was turned on again after settling at a lower value -- physically
   backwards for a vacuum pump, which should only ever pull pressure
   further down from wherever it currently is.
"""
import asyncio
import sys
import time

from pymodbus.simulator import SimDevice, SimData, DataType
from pymodbus.server import StartAsyncTcpServer

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5020  # standard Modbus TCP port

pressure_data = SimData(0, count=1, values=1000, datatype=DataType.REGISTERS)
pump_data = SimData(1, count=1, values=0, datatype=DataType.REGISTERS)

# Shared mutable state the action callback closes over.
state = {"pump_on": False, "pump_started_at": None, "pressure_at_pump_on": None}


# start_address/count are required by pymodbus's own SimDevice(action=...)
# callback signature (called positionally) -- prefixed with _ since they're
# genuinely unused by this particular action.
async def action(
    function_code, _start_address, address, _count, current_registers, set_values
):
    # function_code 6 = write single register (pump on/off command)
    if function_code == 6 and address == 1 and set_values is not None:
        pump_on = bool(set_values[0])
        if pump_on and not state["pump_on"]:
            # Rising edge: capture pressure AT THIS MOMENT as the decay start point,
            # not a hardcoded constant.
            state["pressure_at_pump_on"] = current_registers[0]
            state["pump_started_at"] = time.time()
        elif not pump_on:
            state["pump_started_at"] = None
        state["pump_on"] = pump_on
        print(f"[fake-modbus-vacuum] pump write -> pump_on={pump_on} "
              f"(starting from {state['pressure_at_pump_on']})" if pump_on else
              f"[fake-modbus-vacuum] pump write -> pump_on={pump_on}")

    # function_code 3 = read holding registers (pressure query)
    if function_code == 3 and address == 0:
        if state["pump_on"] and state["pump_started_at"] is not None:
            elapsed = time.time() - state["pump_started_at"]
            start_pressure = state["pressure_at_pump_on"]
            simulated_pressure = max(10, int(start_pressure - elapsed * 50))
            current_registers[0] = simulated_pressure
            print(
                f"[fake-modbus-vacuum] pressure read -> "
                f"{simulated_pressure/1000:.3f} mbar (pump on {elapsed:.1f}s, "
                f"started from {start_pressure/1000:.3f} mbar)"
            )
        else:
            print(
                f"[fake-modbus-vacuum] pressure read -> "
                f"{current_registers[0]/1000:.3f} mbar (pump off)"
            )

    return None


device = SimDevice(id=1, simdata=[pressure_data, pump_data], action=action)


async def main():
    print(f"[fake-modbus-vacuum] listening on {HOST}:{PORT}")
    await StartAsyncTcpServer(device, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())

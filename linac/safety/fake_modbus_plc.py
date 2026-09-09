#!/usr/bin/env python3
"""
linac/safety/interlock1 (Chapter 6) -- a fake PLC-style safety
interlock over Modbus TCP coils. Same abstraction pattern as every
prior linac/ device: the transport is new, the Tango-facing shape
isn't.

This is a supervisory-monitor design, not a device that computes
safety-critical logic in Tango. That's not a simplification for
convenience: real facility architecture (LHC's Beam Interlock System,
ALBA's Equipment Protection System, and a direct real-world example --
a PLC with a Modbus/TCP interface feeding Tango supervisory control at
the PALLAS laser facility) consistently keeps the actual trip logic in
hardwired or PLC hardware, with Tango reading status and gating on it,
never owning it. This simulator plays the role of that PLC; the real
Tango device (interlock_monitor.py) only ever reads it.

Real findings this design depends on, proven standalone first:
  - DataType.BITS, not COILS (no such member exists)
  - genuine coil semantics require SimDevice's 4-tuple simdata form
    (coils, discrete_inputs, holding_registers, input_registers) --
    a flat list silently falls back to register-style handling
  - use_bit_addressing=True needed for natural one-coil-per-address
    behaviour (the default packs 16 bits per register)
  - every one of the 4 tuple positions needs >=1 SimData entry, even
    unused ones -- an empty list crashes __check_block (likely an
    unhandled edge case in the library itself, not our bug)
  - the action callback mechanism (Ch5's pattern) applies uniformly
    to coil function codes 1/5/15, confirmed from simruntime.py's
    _fx_mapper and __check_block -- not register-specific

Coil map:
  0: door_closed     (writable input,  True = safe)
  1: vacuum_ok       (writable input,  True = safe)
  2: water_flow_ok   (writable input,  True = safe)
  3: beam_permit     (read-only,       computed)
  4: fault_latched   (read-only,       computed)
  5: reset           (writable,        momentary, self-clearing)

Safety logic (a real latching pattern, not a plain AND gate): the
instant any input goes unsafe, fault_latched sets and STAYS set even
if the input recovers -- clearing it requires an explicit reset
while all three inputs are currently safe. beam_permit is true only
when inputs are safe AND no fault is latched.
"""
import asyncio
import sys
from pymodbus.simulator import SimData, SimDevice, DataType
from pymodbus.server import StartAsyncTcpServer

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5040

ADDR_DOOR = 0
ADDR_VACUUM = 1
ADDR_WATER = 2
ADDR_PERMIT = 3
ADDR_FAULT = 4
ADDR_RESET = 5

door_closed = SimData(ADDR_DOOR, count=1, values=True, datatype=DataType.BITS)
vacuum_ok = SimData(ADDR_VACUUM, count=1, values=True, datatype=DataType.BITS)
water_flow_ok = SimData(ADDR_WATER, count=1, values=True, datatype=DataType.BITS)
beam_permit = SimData(
    ADDR_PERMIT, count=1, values=True, datatype=DataType.BITS, readonly=True
)
fault_latched = SimData(
    ADDR_FAULT, count=1, values=False, datatype=DataType.BITS, readonly=True
)
reset = SimData(ADDR_RESET, count=1, values=False, datatype=DataType.BITS)

_no_discrete_inputs = SimData(0, count=1, values=False, datatype=DataType.BITS)
_no_holding_registers = SimData(0, count=1, values=0, datatype=DataType.REGISTERS)
_no_input_registers = SimData(0, count=1, values=0, datatype=DataType.REGISTERS)


# Real finding (proven live, not assumed): for a coil/BITS block,
# current_registers is NOT a flat per-address list. It is pymodbus's
# internal PACKED form -- 16 individual coil bits per integer element,
# LSB-first (bit 0 of current_registers[0] = coil address 0), plus a
# trailing 0 padding element the library always appends. Confirmed via
# a live debug print: 6 coils all packed into current_registers[0],
# e.g. 15 == 0b001111 == [door, vacuum, water, permit]=True,
# [fault, reset]=False. Genuinely different from Ch5's register
# callback, where each list index WAS one address's real value.


def _get_bit(registers, bit_index):
    reg_idx, bit_offset = divmod(bit_index, 16)
    return bool((registers[reg_idx] >> bit_offset) & 1)


def _set_bit(registers, bit_index, value):
    reg_idx, bit_offset = divmod(bit_index, 16)
    if value:
        registers[reg_idx] |= 1 << bit_offset
    else:
        registers[reg_idx] &= ~(1 << bit_offset)


async def action(
    function_code, _start_address, address, _count, current_registers, set_values
):
    if function_code not in (5, 15) or set_values is None:
        return None

    # Apply the requested write(s) directly into the packed structure.
    for i, v in enumerate(set_values):
        _set_bit(current_registers, address + i, bool(v))

    if address <= ADDR_RESET < address + len(set_values):
        inputs_ok = (
            _get_bit(current_registers, ADDR_DOOR)
            and _get_bit(current_registers, ADDR_VACUUM)
            and _get_bit(current_registers, ADDR_WATER)
        )
        if _get_bit(current_registers, ADDR_RESET) and inputs_ok:
            _set_bit(current_registers, ADDR_FAULT, False)
        _set_bit(current_registers, ADDR_RESET, False)  # momentary
        # Real finding: mutating current_registers for the SAME address
        # being written does not survive -- pymodbus commits the
        # original requested set_values onto that address afterward,
        # regardless of what the callback did to current_registers.
        # set_values is the mechanism that actually overrides what
        # gets committed for the in-flight write (per the docstring:
        # "update set_values (affect the register update)").
        set_values[ADDR_RESET - address] = False

    inputs_ok = (
        _get_bit(current_registers, ADDR_DOOR)
        and _get_bit(current_registers, ADDR_VACUUM)
        and _get_bit(current_registers, ADDR_WATER)
    )
    if not inputs_ok:
        _set_bit(current_registers, ADDR_FAULT, True)
    _set_bit(
        current_registers,
        ADDR_PERMIT,
        inputs_ok and not _get_bit(current_registers, ADDR_FAULT),
    )

    print(
        f"[fake-plc] door={_get_bit(current_registers, ADDR_DOOR)} "
        f"vacuum={_get_bit(current_registers, ADDR_VACUUM)} "
        f"water={_get_bit(current_registers, ADDR_WATER)} -> "
        f"permit={_get_bit(current_registers, ADDR_PERMIT)} "
        f"fault={_get_bit(current_registers, ADDR_FAULT)}"
    )
    return None


device = SimDevice(
    id=1,
    simdata=(
        [door_closed, vacuum_ok, water_flow_ok, beam_permit, fault_latched, reset],
        [_no_discrete_inputs],
        [_no_holding_registers],
        [_no_input_registers],
    ),
    use_bit_addressing=True,
    action=action,
)


async def main():
    print(f"[fake-plc] listening on {HOST}:{PORT}")
    await StartAsyncTcpServer(device, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())

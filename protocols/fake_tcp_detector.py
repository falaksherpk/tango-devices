#!/usr/bin/env python3
"""
Fake TCP/IP-attached detector for Lab 2.2 (v2.3) -- realistic SCPI-style
instrument simulator with acquisition state, configurable frame rate,
drifting temperature telemetry, a proper SCPI error queue, and
deliberate fault injection for testing a Tango device server's error
handling.

v2.2 -> v2.3 fix:
  - DetectorState() was instantiated at MODULE IMPORT TIME (before
    asyncio.run(main()) starts the event loop), but its __init__
    called asyncio.create_task() for the temperature background task
    -- create_task() requires an already-running event loop.
    RuntimeError: no running event loop, on the very first line of
    the script. Fixed by moving task creation out of __init__ into an
    explicit async start() method, called from main() after the loop
    is already running.

v2 -> v2.1 fixes (found via external review, independently verified
before accepting):
  - SCPI error formatting used repr() (single-quoted), fixed to use
    double quotes matching real SCPI convention.
  - Unrecognized QUERY commands (ending in '?') previously got no
    reply at all, risking a client hang; now they get an immediate
    generic error reply. Non-query unknown commands still get no
    reply, matching real SCPI convention.
  - Simulated temperature used `0.001 * (time.time() % 1000)`, which
    is a sawtooth that resets every 1000 seconds, not a drift.
    Replaced with genuine state updated by an independent background
    task via a small random walk.

v2.1 -> v2.2 fixes (found via a second round of external review,
independently verified before accepting):
  - `reset()` mutated shared state WITHOUT taking the lock, while
    every other mutation path in this class does. Made `reset()`
    async and lock-protected.
  - SIM:FAULT TIMEOUT and SIM:FAULT DISCONNECT were applied BEFORE
    command dispatch, unconditionally -- meaning once either fault
    was active, SIM:FAULT CLEAR itself could never reach the dispatch
    table to turn the fault back off. Fixed by letting any SIM:FAULT
    command bypass the fault-injection check.

Code-quality:
  - asyncio-based (asyncio.start_server), matching the Lab 2.3 Modbus
    simulator's async style.
  - Command dispatch via a lookup table instead of an if/elif chain.
  - Command matching is case-insensitive; a maximum command-line
    length guards the parser against unbounded input.
"""
import asyncio
import random

import redis.asyncio as redis

HOST = "127.0.0.1"
PORT = 5025
MAX_COMMAND_LENGTH = 256
REDIS_STREAM_KEY = "detector:frames"

IDLE = "IDLE"
ACQUIRING = "ACQUIRING"

FAULT_NONE = "NONE"
FAULT_TIMEOUT = "TIMEOUT"
FAULT_DISCONNECT = "DISCONNECT"
FAULT_BAD_RESPONSE = "BAD_RESPONSE"


class DetectorState:
    def __init__(self):
        # Protects shared state accessed from the acquisition task, the
        # temperature task, and however many concurrent client handler
        # coroutines are running -- all within this one asyncio event
        # loop. This coordinates cooperative coroutines within a single
        # thread; it is not a general-purpose thread-safety mechanism.
        self.lock = asyncio.Lock()
        self.acq_state = IDLE
        self.frame_count = 0
        self.frame_rate_hz = 1.0
        self.temperature = 22.0
        self.error_queue = []
        self.fault_mode = FAULT_NONE
        self._acq_task = None
        self._temp_task = None
        self._stream_task = None

    async def start(self):
        """Must be called from within a running event loop -- creates
        the background temperature and Redis-streaming tasks. NOT done
        in __init__, since DetectorState() is constructed at module
        import time, before asyncio.run(main()) has started any event
        loop."""
        self.redis_client = redis.Redis(
            host="127.0.0.1", port=6379, decode_responses=True
        )
        self._temp_task = asyncio.create_task(self._temperature_loop())
        self._stream_task = asyncio.create_task(self._stream_loop())

    async def _stream_loop(self):
        """Independent background task (Lab 3.2): while ACQUIRING,
        pushes the current frame_count and temperature into a Redis
        Stream every second -- a second, independent delivery path
        alongside the existing SCPI/TCP query interface. A real Redis
        consumer and a real SCPI client both see the same underlying
        state, through two completely different mechanisms."""
        try:
            while True:
                await asyncio.sleep(1)
                async with self.lock:
                    if self.acq_state != ACQUIRING:
                        continue
                    frame_count = self.frame_count
                    temperature = self.temperature
                await self.redis_client.xadd(
                    REDIS_STREAM_KEY,
                    {"frame_count": frame_count, "temperature": f"{temperature:.2f}"},
                )
        except asyncio.CancelledError:
            pass

    async def reset(self):
        async with self.lock:
            self.acq_state = IDLE
            self.frame_count = 0
            self.frame_rate_hz = 1.0
            self.temperature = 22.0
            self.error_queue.clear()
            self.fault_mode = FAULT_NONE
            task = self._acq_task
            self._acq_task = None
        if task:
            task.cancel()

    async def start_acquisition(self):
        async with self.lock:
            if self.acq_state == ACQUIRING:
                return
            self.acq_state = ACQUIRING
        self._acq_task = asyncio.create_task(self._acquire_loop())

    async def stop_acquisition(self):
        async with self.lock:
            self.acq_state = IDLE
        if self._acq_task:
            self._acq_task.cancel()
            self._acq_task = None

    async def _acquire_loop(self):
        try:
            while True:
                async with self.lock:
                    if self.acq_state != ACQUIRING:
                        return
                    interval = 1.0 / self.frame_rate_hz
                await asyncio.sleep(interval)
                async with self.lock:
                    if self.acq_state == ACQUIRING:
                        self.frame_count += 1
        except asyncio.CancelledError:
            pass

    async def _temperature_loop(self):
        """Independent background task: a genuine slow random-walk
        drift, with no periodic reset."""
        try:
            while True:
                await asyncio.sleep(2)
                async with self.lock:
                    self.temperature += random.uniform(-0.05, 0.05)
        except asyncio.CancelledError:
            pass

    async def push_error(self, code, message):
        async with self.lock:
            self.error_queue.append((code, message))

    async def pop_error(self):
        async with self.lock:
            if self.error_queue:
                return self.error_queue.pop(0)
            return (0, "No error")


state = DetectorState()


def scpi_error(code, message):
    return f'{code},"{message}"'


async def cmd_idn(_args):
    return "FAKE-DETECTOR-2.3"


async def cmd_rst(_args):
    await state.reset()
    return None


async def cmd_frame_count(_args):
    async with state.lock:
        return str(state.frame_count)


async def cmd_frame_rate_query(_args):
    async with state.lock:
        return f"{state.frame_rate_hz:.2f}"


async def cmd_frame_rate_set(args):
    try:
        rate = float(args)
        if not (0 < rate <= 1000):
            raise ValueError
    except (ValueError, TypeError):
        await state.push_error(-224, "Illegal parameter value")
        return None
    async with state.lock:
        state.frame_rate_hz = rate
    return None


async def cmd_acq_start(_args):
    await state.start_acquisition()
    return None


async def cmd_acq_stop(_args):
    await state.stop_acquisition()
    return None


async def cmd_acq_state(_args):
    async with state.lock:
        return state.acq_state


async def cmd_temp(_args):
    async with state.lock:
        return f"{state.temperature:.2f}"


async def cmd_syst_err(_args):
    code, message = await state.pop_error()
    return scpi_error(code, message)


async def cmd_sim_fault(args):
    valid = {"TIMEOUT", "DISCONNECT", "BAD_RESPONSE", "CLEAR"}
    mode = (args or "").strip().upper()
    if mode not in valid:
        await state.push_error(-224, "Illegal parameter value")
        return None
    async with state.lock:
        state.fault_mode = FAULT_NONE if mode == "CLEAR" else mode
    print(f"[fake-tcp-detector] fault mode set to: {state.fault_mode}")
    return None


DISPATCH = {
    "*IDN?": cmd_idn,
    "*RST": cmd_rst,
    "FRAME:COUNT?": cmd_frame_count,
    "FRAME:RATE?": cmd_frame_rate_query,
    "FRAME:RATE": cmd_frame_rate_set,
    "ACQ:START": cmd_acq_start,
    "ACQ:STOP": cmd_acq_stop,
    "ACQ:STATE?": cmd_acq_state,
    "TEMP?": cmd_temp,
    "SYST:ERR?": cmd_syst_err,
    "SIM:FAULT": cmd_sim_fault,
}


async def handle_client(reader, writer):
    addr = writer.get_extra_info("peername")
    print(f"[fake-tcp-detector] client connected: {addr}")
    try:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=30)
            except TimeoutError:
                continue
            if not line:
                break
            if len(line) > MAX_COMMAND_LENGTH:
                await state.push_error(-223, "Too much data")
                continue

            command_line = line.decode(errors="replace").strip()
            if not command_line:
                continue

            if " " in command_line:
                cmd, args = command_line.split(" ", 1)
            else:
                cmd, args = command_line, None
            cmd = cmd.upper()

            async with state.lock:
                fault = state.fault_mode
            is_fault_control_command = (cmd == "SIM:FAULT")

            if fault == FAULT_DISCONNECT and not is_fault_control_command:
                print(f"[fake-tcp-detector] SIMULATED FAULT: disconnecting {addr}")
                async with state.lock:
                    # Self-clearing: DISCONNECT simulates ONE disconnect
                    # event, not a permanently broken simulator. Without
                    # this, recovering requires a brand-new connection
                    # whose very first command must be SIM:FAULT CLEAR --
                    # too fragile a recovery path, found the hard way when
                    # a second, unrelated test connection immediately hit
                    # a DISCONNECT fault left over from a prior test.
                    state.fault_mode = FAULT_NONE
                break
            if fault == FAULT_TIMEOUT and not is_fault_control_command:
                print(
                    f"[fake-tcp-detector] SIMULATED FAULT: withholding response "
                    f"to {command_line!r}"
                )
                continue

            print(f"[fake-tcp-detector] received: {command_line!r}")
            handler = DISPATCH.get(cmd)

            if handler is None:
                await state.push_error(-113, "Undefined header")
                if cmd.endswith("?"):
                    response = scpi_error(-113, "Undefined header") + "\n"
                    writer.write(response.encode())
                    await writer.drain()
                    print("[fake-tcp-detector] unknown query -> sent error reply")
                else:
                    print(
                        "[fake-tcp-detector] unknown command -> "
                        "pushed error to queue, no reply"
                    )
                continue

            result = await handler(args)

            if (
                fault == FAULT_BAD_RESPONSE
                and result is not None
                and not is_fault_control_command
            ):
                result = "GARBLED#$%DATA"
                print("[fake-tcp-detector] SIMULATED FAULT: sending garbled response")

            if result is not None:
                response = f"{result}\n"
                writer.write(response.encode())
                await writer.drain()
                print(f"[fake-tcp-detector] sent: {response!r}")
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        print(f"[fake-tcp-detector] client disconnected: {addr}")
        writer.close()
        await writer.wait_closed()


async def main():
    # creates the temperature background task, now that the loop is running
    await state.start()
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print(f"[fake-tcp-detector] listening on {HOST}:{PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

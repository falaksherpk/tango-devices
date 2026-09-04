#!/usr/bin/env python3
"""Fake Keithley 6485-style picoammeter, simulating a Faraday cup's current
readout instrument. Speaks a minimal SCPI-like ASCII command set over a
plain TCP socket, one newline-terminated command per line.

This process represents the GPIB *instrument* only. It knows nothing about
GPIB, Prologix, or Tango — it is wrapped by fake_prologix_adapter.py, which
sits in front of it and speaks the actual Prologix command syntax.

Command set verified against real Keithley 6485 documentation:
*IDN?, *RST, SYST:ZCH ON/OFF, SENS:CURR:RANG:AUTO ON, READ?

Usage:
    python3 fake_picoammeter.py <port> [baseline_current_amps]
"""
import asyncio
import contextlib
import logging
import random
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("fake_picoammeter")

# Real 6485 noise floor with zero-check engaged is sub-picoamp; simulate that.
ZERO_CHECK_NOISE_SIGMA = 5e-13
# Normal-operation noise as a fraction of the baseline reading.
NORMAL_NOISE_FRACTION = 0.01


class PicoammeterState:
    """Persists for the life of the simulator process, not per-connection —
    matches how a real GPIB instrument holds state across controller
    reconnects."""

    def __init__(self, baseline_current: float):
        self.baseline_current = baseline_current
        self.zero_check = False


def make_reading(state: PicoammeterState) -> str:
    if state.zero_check:
        value = random.gauss(0.0, ZERO_CHECK_NOISE_SIGMA)
    else:
        sigma = abs(state.baseline_current) * NORMAL_NOISE_FRACTION
        value = random.gauss(state.baseline_current, sigma)
    return f"{value:+.6E}A"


async def handle_command(line: str, state: PicoammeterState) -> str | None:
    """Return a response string for queries, or None for commands that
    don't produce a response (matches real SCPI: only '?' queries reply)."""
    cmd = line.strip()
    upper = cmd.upper()

    if upper == "*IDN?":
        return "KEITHLEY INSTRUMENTS INC.,MODEL 6485,SIMFAB01,B02"
    if upper in ("*RST", "*CLS"):
        if upper == "*RST":
            state.zero_check = False
        return None
    if upper == "SYST:ZCH ON":
        state.zero_check = True
        log.info("zero-check ON")
        return None
    if upper == "SYST:ZCH OFF":
        state.zero_check = False
        log.info("zero-check OFF")
        return None
    if upper == "SENS:CURR:RANG:AUTO ON":
        return None
    if upper == "READ?":
        return make_reading(state)

    log.warning("unrecognized command: %r", cmd)
    if cmd.endswith("?"):
        return "0"  # benign default for an unhandled query, not silence
    return None


async def client_loop(reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter,
                       state: PicoammeterState) -> None:
    peer = writer.get_extra_info("peername")
    log.info("client connected: %s", peer)
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("ascii", errors="replace")
            response = await handle_command(line, state)
            if response is not None:
                writer.write((response + "\n").encode("ascii"))
                await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        log.info("client disconnected: %s", peer)
        writer.close()


async def main(port: int, baseline_current: float) -> None:
    state = PicoammeterState(baseline_current)

    async def handler(reader, writer):
        await client_loop(reader, writer, state)

    server = await asyncio.start_server(handler, "0.0.0.0", port)
    log.info("fake picoammeter listening on port %d (baseline=%.3e A)",
              port, baseline_current)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <port> [baseline_current_amps]")
        sys.exit(1)
    port_arg = int(sys.argv[1])
    baseline_arg = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5e-9
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(port_arg, baseline_arg))

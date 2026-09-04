#!/usr/bin/env python3
"""Fake Prologix GPIB-ETHERNET adapter, sitting in front of a fake GPIB
instrument (e.g. fake_picoammeter.py). Speaks real Prologix controller
command syntax over plain TCP.

Command set and defaults verified against the real Prologix GPIB-ETHERNET
Controller User Manual and independent third-party client implementations:
  ++addr <N>        query/set target GPIB address
  ++mode <0|1>      query/set device(0)/controller(1) mode
  ++auto <0|1>      query/set auto-read-after-write (real factory default: 0)
  ++eos <0-3>       query/set terminator: 0=CR+LF,1=CR,2=LF,3=none (default 0)
  ++read_tmo_ms <N> query/set read timeout in ms
  ++read            explicit read-back (needed when auto=0)
  ++ver             version string

Any line NOT starting with '++' is GPIB pass-through: forwarded to the
currently-addressed instrument, unmodified.

Known simplification (documented, not hidden): our fake backend instrument
speaks newline-terminated ASCII, so eos=0 (CR+LF) or eos=2 (LF) work
correctly; eos=1 (CR only) or eos=3 (none) will NOT reach the backend's
readline()-based parser. A real adapter has no such restriction.

Usage:
    python3 fake_prologix_adapter.py <listen_port> <instrument_host> \\
        <instrument_port> [instrument_gpib_addr]
"""
import asyncio
import contextlib
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("fake_prologix_adapter")

EOS_TERMINATORS = {
    0: "\r\n",
    1: "\r",
    2: "\n",
    3: "",
}


class AdapterState:
    """Persists for the life of the simulator process, matching a real
    adapter's config surviving across client (re)connections."""

    def __init__(self, instrument_addr: int):
        self.addr = instrument_addr
        self.mode = 1  # controller mode (real factory default)
        self.auto = 0  # auto-read-after-write OFF (real factory default)
        self.eos = 0   # CR+LF (real factory default)
        self.read_tmo_ms = 500


class Backend:
    """Single shared connection to the wrapped fake instrument, guarded by
    a lock — a real GPIB bus is one shared channel, not one-per-client."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.lock = asyncio.Lock()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port)
        log.info("connected to backend instrument at %s:%d",
                  self.host, self.port)

    async def write(self, line: str, eos: int):
        term = EOS_TERMINATORS[eos]
        self.writer.write((line + term).encode("ascii"))
        await self.writer.drain()

    async def read(self, timeout_ms: int) -> str | None:
        try:
            raw = await asyncio.wait_for(
                self.reader.readline(), timeout=timeout_ms / 1000)
        except TimeoutError:
            return None
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").rstrip("\r\n")


def parse_plus_plus(cmd: str) -> tuple[str, str | None]:
    parts = cmd.split(None, 1)
    name = parts[0][2:].lower()  # strip '++'
    arg = parts[1] if len(parts) > 1 else None
    return name, arg


async def handle_plus_plus(cmd: str, state: AdapterState,
                            backend: Backend) -> str | None:
    name, arg = parse_plus_plus(cmd)

    if name == "ver":
        return "Prologix GPIB-ETHERNET Controller version 01.05.01.00"
    if name == "addr":
        if arg is None:
            return str(state.addr)
        state.addr = int(arg)
        return None
    if name == "mode":
        if arg is None:
            return str(state.mode)
        state.mode = int(arg)
        return None
    if name == "auto":
        if arg is None:
            return str(state.auto)
        state.auto = int(arg)
        return None
    if name == "eos":
        if arg is None:
            return str(state.eos)
        state.eos = int(arg)
        return None
    if name == "read_tmo_ms":
        if arg is None:
            return str(state.read_tmo_ms)
        state.read_tmo_ms = int(arg)
        return None
    if name == "read":
        async with backend.lock:
            reply = await backend.read(state.read_tmo_ms)
        if reply is None:
            log.warning("++read: no response from instrument within %dms",
                        state.read_tmo_ms)
            return None
        return reply

    log.warning("unrecognized adapter command: %r", cmd)
    return None


async def handle_gpib_passthrough(line: str, state: AdapterState,
                                   backend: Backend,
                                   instrument_addr: int) -> str | None:
    if state.addr != instrument_addr:
        log.warning("addressed device %d has no instrument behind it "
                    "(only %d is wired up) — dropping: %r",
                    state.addr, instrument_addr, line)
        return None

    async with backend.lock:
        await backend.write(line, state.eos)
        if state.auto:
            return await backend.read(state.read_tmo_ms)
    return None


async def client_loop(reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter,
                       state: AdapterState, backend: Backend,
                       instrument_addr: int) -> None:
    peer = writer.get_extra_info("peername")
    log.info("client connected: %s", peer)
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue

            if line.startswith("++"):
                response = await handle_plus_plus(line, state, backend)
            else:
                response = await handle_gpib_passthrough(
                    line, state, backend, instrument_addr)

            if response is not None:
                writer.write((response + "\n").encode("ascii"))
                await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        log.info("client disconnected: %s", peer)
        writer.close()


async def main(listen_port: int, instrument_host: str, instrument_port: int,
                instrument_addr: int) -> None:
    state = AdapterState(instrument_addr)
    backend = Backend(instrument_host, instrument_port)
    await backend.connect()

    async def handler(reader, writer):
        await client_loop(reader, writer, state, backend, instrument_addr)

    server = await asyncio.start_server(handler, "0.0.0.0", listen_port)
    log.info("fake Prologix adapter listening on port %d "
              "(instrument addr %d at %s:%d)",
              listen_port, instrument_addr, instrument_host, instrument_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <listen_port> <instrument_host> "
              f"<instrument_port> [instrument_gpib_addr]")
        sys.exit(1)
    listen_port_arg = int(sys.argv[1])
    instrument_host_arg = sys.argv[2]
    instrument_port_arg = int(sys.argv[3])
    instrument_addr_arg = int(sys.argv[4]) if len(sys.argv) > 4 else 22
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(listen_port_arg, instrument_host_arg,
                          instrument_port_arg, instrument_addr_arg))

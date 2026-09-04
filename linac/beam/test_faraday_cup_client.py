#!/usr/bin/env python3
"""
Manual test client for fake_prologix_adapter.py + fake_picoammeter.py --
proves the Prologix-over-TCP protocol and instrument behavior work in
isolation before any Tango code touches them. Mirrors the same scenarios
manually verified live during Chapter 4 development via raw nc sessions.

Usage:
    python3 test_faraday_cup_client.py [adapter_host] [adapter_port]
"""
import socket
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 1234

FAILURES = []


def send_recv(sock: socket.socket, lines: list[str],
              read_timeout: float = 1.0) -> list[str]:
    """Send each line, then collect whatever response lines arrive within
    read_timeout. Returns only the lines actually received (queries with
    no reply produce nothing, matching the real protocol)."""
    for line in lines:
        sock.sendall((line + "\n").encode("ascii"))
    sock.settimeout(read_timeout)
    responses = []
    buf = b""
    try:
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                responses.append(line.decode("ascii", errors="replace"))
    except TimeoutError:
        pass
    return responses


def check(label: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def drain_stale_responses(sock: socket.socket) -> None:
    """A real finding from live-testing this script: the fake adapter's
    backend link to the instrument is a single persistent connection
    (deliberately, to mirror a real shared GPIB bus), shared across
    every frontend client -- including separate runs of this very
    script. An earlier command from a prior run/session can leave its
    reply sitting unclaimed in that backend pipe if nothing ever issued
    a matching ++read for it, and the NEXT ++read from any client
    (including a brand-new connection) silently receives that stale
    reply instead of its own -- confirmed live when a stale
    current-reading response was returned in place of an expected
    *IDN? reply.

    IMPORTANT CORRECTION (also confirmed live): an earlier version of
    this function drained the wrong socket -- it read from THIS
    script's own fresh frontend connection, which is always empty on a
    new connection and can never contain the stale data. The staleness
    lives in the adapter's backend link to the instrument, not in any
    frontend client's own socket, so it can only be cleared through
    the real protocol: issue ++read with a short timeout and discard
    whatever comes back (if anything), which asks the adapter itself
    to consume and drop any pending backend reply."""
    sock.sendall(b"++read_tmo_ms 100\n")
    sock.sendall(b"++read\n")
    sock.settimeout(0.3)
    drained = []
    try:
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            drained.append(chunk)
    except TimeoutError:
        pass
    if drained:
        print(f"[test-client] drained stale backend reply via ++read: {drained}")
    # Restore the real default timeout before the actual test scenarios run.
    sock.sendall(b"++read_tmo_ms 500\n")


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((HOST, PORT))
        print(f"[test-client] connected to {HOST}:{PORT}")
        drain_stale_responses(sock)

        # Scenario 1: version + correct-address identify, explicit ++read
        # (auto=0 is the real factory default -- confirmed against real
        # Prologix docs -- so *IDN? produces no immediate reply).
        resp = send_recv(sock, ["++ver"])
        check("++ver returns version string",
              len(resp) == 1 and "Prologix GPIB-ETHERNET" in resp[0],
              detail=repr(resp))

        resp = send_recv(sock, ["++addr 22", "*IDN?"])
        check("*IDN? with auto=0 produces no immediate reply",
              resp == [], detail=repr(resp))

        resp = send_recv(sock, ["++read"])
        check("++read fetches the pending *IDN? response",
              len(resp) == 1 and "KEITHLEY" in resp[0], detail=repr(resp))

        # Scenario 2: zero-check state change is real, not just accepted
        resp = send_recv(sock, ["SYST:ZCH ON", "READ?", "++read"])
        check("READ? after ZCH ON returns near-zero (zero-check noise floor)",
              len(resp) == 1 and abs(float(resp[0].rstrip("A"))) < 1e-11,
              detail=repr(resp))

        resp = send_recv(sock, ["SYST:ZCH OFF", "READ?", "++read"])
        check("READ? after ZCH OFF returns near baseline (~2.5e-9 A)",
              len(resp) == 1 and abs(float(resp[0].rstrip("A")) - 2.5e-9) < 5e-10,
              detail=repr(resp))

        # Scenario 3: wrong address is silently dropped (no instrument
        # there), confirmed distinct from a real timeout.
        resp = send_recv(sock, ["++addr 5", "READ?"])
        check("wrong address produces no reply (dropped, not forwarded)",
              resp == [], detail=repr(resp))

        # Scenario 4: ++read with nothing pending times out silently
        # (proves the timeout path, not just the happy path).
        resp = send_recv(sock, ["++addr 22", "++read_tmo_ms 200", "++read"],
                          read_timeout=0.5)
        check("++read with nothing pending times out with no reply",
              resp == [], detail=repr(resp))

        # Scenario 5: ++auto 1 makes a write immediately produce a reply,
        # no explicit ++read needed -- the real behavioral difference
        # ++auto is supposed to make.
        resp = send_recv(sock, ["++auto 1", "READ?"])
        check("++auto 1 makes READ? reply immediately, no ++read needed",
              len(resp) == 1, detail=repr(resp))

        # Restore auto=0 (real factory default) before disconnecting, so
        # this script leaves the shared fake hardware in its default
        # state for whatever runs next against it.
        send_recv(sock, ["++auto 0"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    else:
        print("All checks passed.")


if __name__ == "__main__":
    main()

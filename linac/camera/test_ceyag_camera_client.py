#!/usr/bin/env python3
"""
Manual test client for fake_ceyag_camera.py -- proves the TCP framing
protocol works in isolation before any Tango code touches it.
"""
import socket
import struct
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9999
NUM_FRAMES = int(sys.argv[3]) if len(sys.argv) > 3 else 3

MAGIC = b"CEYG"


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes, since a single recv() call is not
    guaranteed to return the full amount requested over TCP."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed before expected bytes arrived")
        buf += chunk
    return buf


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((HOST, PORT))
        print(f"[test-client] connected to {HOST}:{PORT}")

        for i in range(NUM_FRAMES):
            magic = recv_exact(sock, 4)
            if magic != MAGIC:
                print(f"[test-client] BAD MAGIC: got {magic!r}, expected {MAGIC!r}")
                return
            (length,) = struct.unpack(">I", recv_exact(sock, 4))
            payload = recv_exact(sock, length)
            print(
                f"[test-client] frame {i}: magic OK, length={length}, "
                f"first byte={payload[0]}, last byte={payload[-1]}"
            )


if __name__ == "__main__":
    main()

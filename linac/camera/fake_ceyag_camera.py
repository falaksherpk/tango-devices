#!/usr/bin/env python3
"""
Fake Ce:YAG camera simulator for Part 3 Chapter 3.

Deliberately simple: listens on a TCP port and continuously streams
synthetic 640x480 grayscale frames to whichever client is currently
connected, on a fixed interval. Real camera control/acquisition logic
belongs in the Tango device server, not here -- same transport/logic
split already used in every fake hardware simulator in this project.

Serves one client at a time, but loops accept() indefinitely so a new
client (e.g. the real device server restarting) can reconnect without
this simulator itself needing to be restarted -- matching how a real
camera behaves: the hardware doesn't vanish just because its one
current client dropped the connection.

Wire protocol (one frame):
    4 bytes  magic       b"CEYG"
    4 bytes  length      big-endian uint32, payload length in bytes
    N bytes  payload     raw grayscale pixels, row-major, 1 byte/pixel
"""
import socket
import struct
import sys
import time

HOST = "0.0.0.0"
WIDTH = 640
HEIGHT = 480
MAGIC = b"CEYG"


def make_frame(frame_number: int) -> bytes:
    """Synthetic grayscale frame: a horizontal gradient that shifts
    each frame, so consecutive frames are visibly different -- easy
    to spot a stuck/frozen stream during manual testing."""
    shift = frame_number % 256
    row = bytes((x + shift) % 256 for x in range(WIDTH))
    return row * HEIGHT


def serve_one_client(conn: socket.socket, addr, frame_interval_s: float) -> None:
    print(f"[fake-ceyag-camera] client connected: {addr}")
    frame_number = 0
    try:
        with conn:
            while True:
                payload = make_frame(frame_number)
                header = MAGIC + struct.pack(">I", len(payload))
                conn.sendall(header + payload)
                print(
                    f"[fake-ceyag-camera] sent frame {frame_number} "
                    f"({len(payload)} bytes)"
                )
                frame_number += 1
                time.sleep(frame_interval_s)
    except (BrokenPipeError, ConnectionResetError):
        print("[fake-ceyag-camera] client disconnected")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    frame_interval_s = float(sys.argv[2]) if len(sys.argv) > 2 else 0.2

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, port))
    server.listen(1)
    print(f"[fake-ceyag-camera] listening on {HOST}:{port}")

    try:
        while True:
            conn, addr = server.accept()
            serve_one_client(conn, addr, frame_interval_s)
            print("[fake-ceyag-camera] waiting for next client...")
    except KeyboardInterrupt:
        print("[fake-ceyag-camera] shutting down")


if __name__ == "__main__":
    main()

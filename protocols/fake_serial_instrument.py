#!/usr/bin/env python3
"""
Fake RS-232 instrument for Lab 2.1.
Listens on one end of a socat virtual serial pair, responds to *IDN?
like a real bench instrument would over a real RS-232 line.
"""
import serial
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/pts/1"

def main():
    ser = serial.Serial(PORT, baudrate=9600, timeout=None)
    print(f"[fake-instrument] listening on {PORT}")

    buf = b""
    while True:
        chunk = ser.read(1)  # blocking read, 1 byte at a time
        if not chunk:
            continue
        buf += chunk
        if buf.endswith(b"\n") or buf.endswith(b"\r"):
            command = buf.strip().decode(errors="replace")
            buf = b""
            if not command:
                # Bare CR or LF terminator byte with nothing before it
                # (e.g. the second half of a \r\n pair) -- not a real command.
                continue
            print(f"[fake-instrument] received: {command!r}")
            if command == "*IDN?":
                response = "FAKE-INSTRUMENT-1.0\r\n"
                ser.write(response.encode())
                print(f"[fake-instrument] sent: {response!r}")
            else:
                ser.write(b"ERR unknown command\r\n")
                print("[fake-instrument] sent: 'ERR unknown command'")

if __name__ == "__main__":
    main()

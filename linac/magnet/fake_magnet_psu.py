#!/usr/bin/env python3
"""
Fake RS-232 magnet power supply for Part 3 Chapter 2.

Deliberately simple: this simulator only stores/reports current and
setpoint values via SCPI-style commands. The actual ramp PHYSICS
(finite ramp rate, MOVING state, settling behavior) belongs in the
Tango device server (magnet_power_supply.py), not here -- keeping the
simulator and the "real" ramp logic cleanly separated, the same
transport/logic split already used in every Phase 2 protocol lab.
"""
import serial
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/pts/1"


def main():
    ser = serial.Serial(PORT, baudrate=9600, timeout=None)
    print(f"[fake-magnet-psu] listening on {PORT}")

    current = 0.0

    buf = b""
    while True:
        chunk = ser.read(1)
        if not chunk:
            continue
        buf += chunk
        if buf.endswith(b"\n") or buf.endswith(b"\r"):
            command_line = buf.strip().decode(errors="replace")
            buf = b""
            if not command_line:
                continue
            print(f"[fake-magnet-psu] received: {command_line!r}")

            if command_line == "*IDN?":
                response = "FAKE-MAGNET-PSU-1.0\r\n"
                ser.write(response.encode())
            elif command_line == "CURR?":
                response = f"{current:.4f}\r\n"
                ser.write(response.encode())
            elif command_line.startswith("CURR "):
                try:
                    current = float(command_line.split(" ", 1)[1])
                    response = None
                except ValueError:
                    response = "ERR bad value\r\n"
                    ser.write(response.encode())
                    continue
                print(
                    f"[fake-magnet-psu] current register set directly to "
                    f"{current:.4f} A"
                )
                continue
            else:
                response = "ERR unknown command\r\n"
                ser.write(response.encode())
                print(f"[fake-magnet-psu] sent: {response!r}")
                continue

            print(f"[fake-magnet-psu] sent: {response!r}")


if __name__ == "__main__":
    main()

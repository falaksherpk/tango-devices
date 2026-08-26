#!/usr/bin/env python3
"""
Manual test client for fake_magnet_psu.py -- proves the RS-232
transport works in isolation before any Tango code touches it.
"""
import serial
import sys
import time

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/pts/2"


def query(ser, command_str, expect_reply=True):
    ser.write((command_str + "\r\n").encode())
    if not expect_reply:
        return None
    return ser.readline().decode(errors="replace").strip()


def main():
    ser = serial.Serial(PORT, baudrate=9600, timeout=2)
    print(f"[test-client] opened {PORT}")

    print("*IDN? ->", query(ser, "*IDN?"))
    print("CURR? (before set) ->", query(ser, "CURR?"))

    query(ser, "CURR 35.5", expect_reply=False)
    time.sleep(0.2)
    print("CURR? (after CURR 35.5) ->", query(ser, "CURR?"))

    print("BAD:COMMAND ->", query(ser, "BAD:COMMAND"))

    ser.close()


if __name__ == "__main__":
    main()

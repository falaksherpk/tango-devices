#!/usr/bin/env python3
"""
Quick manual test client for Lab 2.1 — sends *IDN? to the fake instrument
over the other end of the socat virtual serial pair, prints the reply.
"""
import serial
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/pts/2"

def main():
    ser = serial.Serial(PORT, baudrate=9600, timeout=2)
    print(f"[test-client] opened {PORT}")

    ser.write(b"*IDN?\r\n")
    print("[test-client] sent: '*IDN?'")

    response = ser.readline()
    print(f"[test-client] received: {response!r}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Quick manual test client for Lab 2.3 -- reads pressure, turns the pump
on, then reads pressure again after a delay to prove it's genuinely
dropping over time (driven by the fake controller's action callback).
"""
import time
from pymodbus.client import ModbusTcpClient

HOST = "127.0.0.1"
PORT = 5020


def main():
    c = ModbusTcpClient(HOST, port=PORT)
    c.connect()
    print("[test-client] connected")

    r0 = c.read_holding_registers(address=0, count=1, device_id=1)
    print(f"[test-client] pressure before pump on: {r0.registers[0]/1000:.3f} mbar")

    c.write_register(address=1, value=1, device_id=1)
    print("[test-client] pump turned ON")

    time.sleep(3)
    r1 = c.read_holding_registers(address=0, count=1, device_id=1)
    print(f"[test-client] pressure 3s after pump on: {r1.registers[0]/1000:.3f} mbar")

    time.sleep(3)
    r2 = c.read_holding_registers(address=0, count=1, device_id=1)
    print(f"[test-client] pressure 6s after pump on: {r2.registers[0]/1000:.3f} mbar")

    c.write_register(address=1, value=0, device_id=1)
    print("[test-client] pump turned OFF")

    c.close()

    if r2.registers[0] < r1.registers[0] < r0.registers[0]:
        print("[test-client] CONFIRMED: pressure is genuinely dropping toward vacuum over time")
    else:
        print("[test-client] WARNING: pressure did not drop as expected")


if __name__ == "__main__":
    main()

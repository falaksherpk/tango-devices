#!/usr/bin/env python3
"""
Manual verification client for fake_tcp_detector.py v2.3 -- exercises
acquisition state, frame rate, temperature drift, the SCPI error
queue, unknown-command handling, and all three fault-injection modes.
"""
import socket
import time

HOST = "127.0.0.1"
PORT = 5025


def query(sock, command_str, expect_reply=True):
    sock.sendall((command_str + "\n").encode())
    if not expect_reply:
        return None
    sock.settimeout(3)
    try:
        return sock.recv(1024).decode(errors="replace").strip()
    except TimeoutError:
        return "<<NO REPLY -- TIMED OUT>>"


def new_connection():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PORT))
    return s


def main():
    s = new_connection()
    print("=== Basic identity and reset ===")
    print("*IDN?  ->", query(s, "*IDN?"))
    print("*RST   ->", query(s, "*RST", expect_reply=False))

    print("\n=== Acquisition state machine ===")
    print("ACQ:STATE?          ->", query(s, "ACQ:STATE?"))
    print("FRAME:COUNT? (idle) ->", query(s, "FRAME:COUNT?"))
    query(s, "ACQ:START", expect_reply=False)
    print("ACQ:STATE? (after START) ->", query(s, "ACQ:STATE?"))
    time.sleep(2.5)
    print("FRAME:COUNT? (after 2.5s @ 1Hz) ->", query(s, "FRAME:COUNT?"))

    print("\n=== Configurable frame rate ===")
    query(s, "FRAME:RATE 5", expect_reply=False)
    print("FRAME:RATE? ->", query(s, "FRAME:RATE?"))
    before = int(query(s, "FRAME:COUNT?"))
    time.sleep(2)
    after = int(query(s, "FRAME:COUNT?"))
    print(f"frames in 2s @ 5Hz: {after - before} (expect ~10)")
    query(s, "ACQ:STOP", expect_reply=False)

    print("\n=== Temperature (no sawtooth, should drift smoothly) ===")
    t1 = query(s, "TEMP?")
    time.sleep(3)
    t2 = query(s, "TEMP?")
    print(f"TEMP? t1={t1}  t2={t2}")

    print("\n=== Error queue and bad parameter ===")
    query(s, "FRAME:RATE -5", expect_reply=False)  # illegal value
    print("SYST:ERR? (expect -224) ->", query(s, "SYST:ERR?"))
    print("SYST:ERR? (expect 0, No error) ->", query(s, "SYST:ERR?"))

    print("\n=== Unknown command handling ===")
    print(
        "BAD:COMMAND?  (query, expect immediate error reply) ->",
        query(s, "BAD:COMMAND?"),
    )
    query(s, "BAD:COMMAND", expect_reply=False)  # non-query, no reply expected
    print("SYST:ERR? (should show the BAD:COMMAND error) ->", query(s, "SYST:ERR?"))

    print("\n=== Case-insensitivity ===")
    print("*idn? (lowercase) ->", query(s, "*idn?"))

    s.close()

    print("\n=== Fault injection: TIMEOUT ===")
    s2 = new_connection()
    query(s2, "SIM:FAULT TIMEOUT", expect_reply=False)
    print("TEMP? while TIMEOUT active ->", query(s2, "TEMP?"))
    print(
        "SIM:FAULT CLEAR (must bypass the fault) ->",
        query(s2, "SIM:FAULT CLEAR", expect_reply=False),
    )
    print("TEMP? after CLEAR ->", query(s2, "TEMP?"))
    s2.close()

    print("\n=== Fault injection: BAD_RESPONSE ===")
    s3 = new_connection()
    query(s3, "SIM:FAULT BAD_RESPONSE", expect_reply=False)
    print("TEMP? while BAD_RESPONSE active ->", query(s3, "TEMP?"))
    query(s3, "SIM:FAULT CLEAR", expect_reply=False)
    print("TEMP? after CLEAR ->", query(s3, "TEMP?"))
    s3.close()

    print("\n=== Fault injection: DISCONNECT ===")
    s4 = new_connection()
    query(s4, "SIM:FAULT DISCONNECT", expect_reply=False)
    print("Sending TEMP? -- expect connection to drop...")
    try:
        result = query(s4, "TEMP?")
        print("TEMP? ->", result)
    except (ConnectionResetError, BrokenPipeError) as e:
        print(f"Connection dropped as expected: {e}")
    s4.close()


if __name__ == "__main__":
    main()

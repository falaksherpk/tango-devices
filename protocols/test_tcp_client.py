#!/usr/bin/env python3
"""
Quick manual test client for Lab 2.2 -- queries the fake TCP detector
twice with a delay in between, to prove the frame counter is genuinely
incrementing over time, not just returning a fixed value.
"""
import socket
import time

HOST = "127.0.0.1"
PORT = 5025


def query(sock, command_str):
    sock.sendall((command_str + "\n").encode())
    response = sock.recv(1024).decode(errors="replace").strip()
    return response


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        print("[test-client] connected")

        print("[test-client] *IDN? ->", query(s, "*IDN?"))

        first = query(s, "FRAME:COUNT?")
        print("[test-client] FRAME:COUNT? ->", first)

        print("[test-client] sleeping 3s to let frame count advance...")
        time.sleep(3)

        second = query(s, "FRAME:COUNT?")
        print("[test-client] FRAME:COUNT? ->", second)

        if int(second) > int(first):
            print("[test-client] CONFIRMED: frame count is genuinely incrementing")
        else:
            print("[test-client] WARNING: frame count did not increase")


if __name__ == "__main__":
    main()

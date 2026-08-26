#!/usr/bin/env python3
"""Tiny helper: sets a SIM:FAULT mode on the running simulator, then exits."""
import socket
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "CLEAR"
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect(("127.0.0.1", 5025))
s.sendall(f"SIM:FAULT {mode}\n".encode())
s.close()
print(f"Set fault mode: {mode}")

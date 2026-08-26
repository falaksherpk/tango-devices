#!/usr/bin/env python3
"""
Lab 3.1 -- plain ZMQ PUB/SUB, no Tango involved yet.

Publishes fake "detector frame" events (a counter + timestamp) at 1Hz
over a ZMQ PUB socket. Any number of subscribers can connect and
receive every published message -- PUB/SUB is a broadcast pattern,
not point-to-point.
"""
import time
import zmq

PORT = 5556

def main():
    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind(f"tcp://*:{PORT}")
    print(f"[publisher] bound to tcp://*:{PORT}")

    # PUB sockets have no concept of "connected" -- a brief pause lets
    # any subscriber that starts around the same time finish its
    # connection handshake before we start sending. Messages published
    # before a subscriber connects are simply never received (PUB/SUB
    # has no message buffering/replay for late joiners).
    time.sleep(1)

    frame_count = 0
    try:
        while True:
            frame_count += 1
            message = f"frame.count {frame_count} {time.time():.3f}"
            socket.send_string(message)
            print(f"[publisher] sent: {message!r}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("[publisher] shutting down")
    finally:
        socket.close()
        context.term()

if __name__ == "__main__":
    main()

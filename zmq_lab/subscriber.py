#!/usr/bin/env python3
"""
Lab 3.1 -- plain ZMQ SUB counterpart to publisher.py.

Connects to the publisher and subscribes to ALL messages (empty topic
filter). ZMQ PUB/SUB uses a topic-prefix filter -- subscribing with ""
matches every message regardless of its content, since every string
has "" as a prefix.
"""
import zmq

PORT = 5556

def main():
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(f"tcp://127.0.0.1:{PORT}")
    socket.setsockopt_string(zmq.SUBSCRIBE, "")  # "" = subscribe to everything
    print(f"[subscriber] connected to tcp://127.0.0.1:{PORT}, subscribed to all topics")

    try:
        while True:
            message = socket.recv_string()
            print(f"[subscriber] received: {message!r}")
    except KeyboardInterrupt:
        print("[subscriber] shutting down")
    finally:
        socket.close()
        context.term()

if __name__ == "__main__":
    main()

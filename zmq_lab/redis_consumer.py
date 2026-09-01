#!/usr/bin/env python3
"""
Lab 3.2 -- Redis Streams consumer using a consumer group, reading
frame/temperature data pushed by fake_tcp_detector.py's _stream_loop().

Demonstrates the actual value of a consumer group over plain XREAD:
explicit acknowledgment (XACK) after successful processing, so an
unacknowledged message (e.g. from a consumer that crashed mid-process)
remains claimable rather than being silently lost.
"""
import redis

STREAM_KEY = "detector:frames"
GROUP_NAME = "archivers"
CONSUMER_NAME = "archiver-1"


def main():
    # socket_timeout is a CLIENT-side socket read timeout. redis-py
    # 8.x defaults this to 5 seconds -- if it's <= our BLOCK duration
    # below, the client's own read can time out at nearly the same
    # moment the server would otherwise return an empty result after
    # its BLOCK period, racing against it (confirmed live: this raced
    # and raised redis.exceptions.TimeoutError after ~58s of internal
    # retries). Blocking commands should be timed by BLOCK, not by the
    # client socket -- set socket_timeout comfortably longer than the
    # longest BLOCK value used anywhere in this script.
    r = redis.Redis(
        host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=10
    )

    # Create the consumer group if it doesn't already exist.
    # mkstream=True: create the stream itself too, if it doesn't exist
    # yet (e.g. if no producer has pushed anything at all so far).
    # id="0": start the group at the beginning of the stream, so it
    # sees everything, not just messages pushed after the group exists.
    try:
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
        print(f"[consumer] created consumer group {GROUP_NAME!r}")
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            print(
                f"[consumer] consumer group {GROUP_NAME!r} already exists, reusing it"
            )
        else:
            raise

    print(
        f"[consumer] reading as {CONSUMER_NAME!r} in group {GROUP_NAME!r} "
        f"(Ctrl-C to stop)..."
    )

    try:
        while True:
            # ">" means: give me only NEW messages, never yet delivered
            # to any consumer in this group. block=5000: wait up to 5s
            # for new data before looping again (lets Ctrl-C work
            # promptly instead of blocking forever).
            response = r.xreadgroup(
                GROUP_NAME, CONSUMER_NAME,
                {STREAM_KEY: ">"},
                count=10, block=5000,
            )
            if not response:
                continue

            for _stream_key, messages in response:
                for message_id, fields in messages:
                    print(f"[consumer] processing {message_id}: {fields}")
                    # ... real processing (e.g. writing to a database)
                    # would happen here ...
                    r.xack(STREAM_KEY, GROUP_NAME, message_id)
                    print(f"[consumer] acknowledged {message_id}")
    except KeyboardInterrupt:
        print("[consumer] shutting down")


if __name__ == "__main__":
    main()

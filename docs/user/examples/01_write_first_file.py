"""Tutorial stage 1: write a first ``.zpf`` file.

One session, two participants (a client and a server), one request/response
pair. Run with ``python 01_write_first_file.py``; it writes ``session.zpf``
into the current directory for the next stage to read.
"""

import os

import zpf

with zpf.create("session.zpf", tick_hz=1_000_000) as writer:
    capture = writer.add_source("capture", uri="capture.pcap")

    with writer.begin_session(proto="tcp", key="10.0.0.1:51000 <-> 93.184.216.34:80") as session:
        client = session.participant("10.0.0.1:51000", isn=1000)
        server = session.participant("93.184.216.34:80", isn=5000)

        # The client's SYN carried isn=1000, so its first application byte is
        # seq_start=1001; "GET / HTTP/1.1\r\n\r\n" is 18 bytes, so it runs up
        # to (but not including) byte 1019.
        session.record(client, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n", seq_start=1001, ack=5001)
        # The server's response acknowledges the whole request (ack=1019)
        # and starts its own stream at seq_start=5001 (isn=5000 + 1).
        session.record(
            server,
            ts=1005,
            payload=b"HTTP/1.1 200 OK\r\n\r\nhi",
            seq_start=5001,
            ack=1019,
        )
        # session.end() is implicit here (clean exit of the `with` block);
        # writer's End block follows the same way when `writer`'s block exits.

print(f"wrote session.zpf ({os.path.getsize('session.zpf')} bytes)")

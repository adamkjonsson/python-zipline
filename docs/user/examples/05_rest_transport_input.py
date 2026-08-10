"""Decoder tutorial stage 1: the raw input a decoder consumes.

Writes ``rest_transport.zpf`` -- a raw capture of one REST call (a GET and its
JSON response) -- then reads it back to show what a decoder sees: two
participant streams of byte-run records, and the logical stream offset of
each record. Run with ``python 05_rest_transport_input.py``; the next stage reads
the file it leaves behind.
"""

import zpf

CLIENT_ISN = 1000
SERVER_ISN = 5000

# The client's request, deliberately split across two byte-run records: on
# the wire a message rarely lands in one neat chunk, and the decoder is what
# puts it back together.
REQ_1 = b"GET /users/1 HTTP/1.1\r\nHost: api.example.com\r\n"
REQ_2 = b"Accept: application/json\r\n\r\n"

BODY = b'{"id":1,"name":"Ada Lovelace"}'
RESP = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(BODY)).encode() + b"\r\n\r\n" + BODY
)

with zpf.create("rest_transport.zpf", tick_hz=1_000_000) as writer:
    writer.add_source("capture", uri="rest.pcap")
    with writer.begin_session(
        proto="tcp", key="10.0.0.1:51000 <-> 93.184.216.34:80", session_id=7
    ) as session:
        client = session.participant("10.0.0.1:51000", isn=CLIENT_ISN)
        server = session.participant("93.184.216.34:80", isn=SERVER_ISN)
        session.record(client, ts=1, payload=REQ_1, seq_start=CLIENT_ISN + 1)
        session.record(client, ts=2, payload=REQ_2, seq_start=CLIENT_ISN + 1 + len(REQ_1))
        session.record(
            server,
            ts=3,
            payload=RESP,
            seq_start=SERVER_ISN + 1,
            ack=CLIENT_ISN + 1 + len(REQ_1) + len(REQ_2),
        )

# Read it back the way a decoder does: walk every session, one reassembly
# view per stream. `reassemble()` turns each participant's byte-run records
# into logical-offset segments, so the decoder never touches seq_start/isn
# arithmetic itself. (This file holds one session, but a decoder handles
# whatever the input contains.)
with zpf.open("rest_transport.zpf") as reader:
    for session in reader.sessions():
        for stream in session.reassemble():
            participant = stream.participant
            print(f"participant {participant.participant_id} ({participant.endpoint}):")
            # The individual records, each at its logical stream offset...
            for datagram in stream.datagrams():
                print(f"  offset {datagram.off_start:>3}: {datagram.data!r}")
            # ...coalesced into the contiguous runs the decoder actually parses.
            for segment in stream.segments():
                print(f"  reassembled [{segment.off_start}, {segment.off_end})")

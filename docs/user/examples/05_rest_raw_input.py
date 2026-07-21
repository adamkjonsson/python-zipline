"""Decoder tutorial stage 1: the raw input a decoder consumes.

Writes ``rest_raw.zpf`` -- a raw capture of one REST call (a GET and its
JSON response) -- then reads it back to show what a decoder sees: two
participant streams of byte-run records, and the logical stream offset of
each record. Run with ``python 05_rest_raw_input.py``; the next stage reads
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

with zpf.create("rest_raw.zpf", tick_hz=1_000_000) as writer:
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

# Read it back and show each stream the way the decoder will consume it.
with zpf.open("rest_raw.zpf") as reader:
    (session,) = reader.sessions()
    for participant in session.participants:
        pid = participant.participant_id
        origin = participant.isn + 1  # the stream's first byte is isn + 1
        print(f"participant {pid} ({participant.endpoint}):")
        for record in session.stream(pid):
            offset = record.seq_start - origin  # logical 0-based stream offset
            print(f"  offset {offset:>3}: {record.payload!r}")

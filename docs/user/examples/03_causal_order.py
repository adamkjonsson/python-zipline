"""Tutorial stage 3: causal order beats wall-clock order.

Two demonstrations, self-contained (no files from earlier stages needed):

1. One session, two participants, an ack tying their records together.
   Sorting by timestamp gets the order backwards; ``timeline()`` doesn't.
2. The realistic version of the same problem: the two directions were
   captured separately, at different tap points with skewed clocks, as
   ``sideA.zpf``/``sideB.zpf``. ``zpf.merge_files`` runs the same causal
   merge once and bakes the result into a SEQUENCED file.
"""

import io

import zpf

# --- 1. timeline() vs. sorting by timestamp -----------------------------

buffer = io.BytesIO()
with zpf.create(buffer, tick_hz=1_000_000) as writer:
    writer.add_source("capture", uri="capture.pcap")
    with writer.begin_session(proto="tcp") as session:
        client = session.participant("10.0.0.1:51000", isn=1000)
        server = session.participant("93.184.216.34:80", isn=5000)
        # The server's clock runs behind the client's, so its response is
        # timestamped *before* the request it answers -- even though its
        # ack proves it was written afterwards.
        session.record(client, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n", seq_start=1001)
        session.record(
            server, ts=995, payload=b"HTTP/1.1 200 OK\r\n\r\nhi", seq_start=5001, ack=1019
        )

buffer.seek(0)
with zpf.open(buffer) as reader:
    (session,) = reader.sessions()

    by_timestamp = sorted(session.records(), key=lambda r: r.timestamp)
    print("sorted by timestamp:", [r.payload[:3] for r in by_timestamp])  # wrong: response first

    causal_order = list(session.timeline())
    print("session.timeline():", [r.payload[:3] for r in causal_order])  # right: request first

# --- 2. the same problem, captured as two separate files ----------------

KEY = "10.0.0.1:51000 <-> 93.184.216.34:80"

with zpf.create("sideA.zpf", tick_hz=1_000_000) as writer:
    writer.add_source("capture", uri="sideA.pcap")
    with writer.begin_session(proto="tcp", key=KEY) as side_a:
        client = side_a.participant("10.0.0.1:51000", isn=1000)
        side_a.record(client, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n", seq_start=1001, ack=5001)

with zpf.create("sideB.zpf", tick_hz=1_000_000) as writer:
    writer.add_source("capture", uri="sideB.pcap")
    with writer.begin_session(proto="tcp", key=KEY) as side_b:
        server = side_b.participant("93.184.216.34:80", isn=5000)
        # Same skewed clock as demonstration 1, now on its own tap point.
        side_b.record(
            server, ts=995, payload=b"HTTP/1.1 200 OK\r\n\r\nhi", seq_start=5001, ack=1019
        )

zpf.merge_files("sideA.zpf", "sideB.zpf", "merged.zpf", produced_by="tutorial 1.0")

with zpf.open("merged.zpf") as merged:
    (session,) = merged.sessions()
    print(f"merged session sequenced: {session.sequenced}")
    print("merged order:", [r.payload[:3] for r in session.records()])  # request, then response
    session.verify()  # raises if the stored order is not a causal linearization
    print("verify(): OK")

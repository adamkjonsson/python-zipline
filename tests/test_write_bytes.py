"""The write path's bytes, pinned (#101).

#101 makes writing a record cheaper by changing how a Record is validated,
its flags tested and its options encoded. None of that may move a byte. So
this writes a fixed workload covering every Record option and the flags the
encoder and the checker test, and compares the output with digests taken
**before** the change. A digest that moves is a change to the format's bytes,
which #101 must never make.
"""

from __future__ import annotations

import hashlib
import io

import zpf

PSH, FIN, SYN = zpf.RecordFlags.PSH, zpf.RecordFlags.FIN, zpf.RecordFlags.SYN
MESSAGE, COPY = zpf.RecordFlags.MESSAGE, zpf.RecordFlags.COPY


def capture_file() -> bytes:
    """Write a capture-sourced file: TCP with every hint, and UDP with a copy."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000, creator="pin 1") as writer:
        writer.add_source("capture", uri="pin.pcap")
        reassembly = writer.add_decoder(
            "tcp-reassembly", version="1", output_layer=zpf.OutputLayer.TRANSPORT
        )
        with writer.begin_session(proto="tcp", key="c <-> s", session_id=7) as tcp:
            client = tcp.participant("10.0.0.1:51000", isn=1000)
            server = tcp.participant("93.184.216.34:80", isn=5000)
            tcp.record(client, ts=900, flags=SYN, hints=zpf.Hints(seq_start=1001))
            tcp.record(server, ts=950, flags=SYN, hints=zpf.Hints(seq_start=5001, ack=1001))
            seq, ack = 1001, 5001
            for k in range(200):
                payload = bytes((k + i) % 256 for i in range(k % 37))
                tcp.record(
                    client,
                    ts=1000 + 10 * k,
                    ts_first=(995 + 10 * k) if k % 2 else None,
                    payload=payload,
                    flags=PSH if k % 3 else 0,
                    hints=zpf.Hints(seq_start=seq, ack=ack if k % 5 else None),
                    decoded=zpf.Decoded(decoder=reassembly) if k % 4 else None,
                    comment="note" if k % 50 == 0 else None,
                )
                seq += len(payload)
            tcp.record(server, ts=4000, payload=b"bye", flags=PSH | FIN,
                       hints=zpf.Hints(seq_start=ack, ack=seq))
            tcp.record(client, ts=4100, hints=zpf.Hints(seq_start=seq, ack=ack + 4))
        with writer.begin_session(proto="udp", session_id=9) as udp:
            server = udp.participant("10.0.0.2:53")
            udp.record(server, ts=5000, payload=b"answer", flags=MESSAGE)
            udp.record(server, ts=5001, payload=b"answer", flags=MESSAGE | COPY)
    return sink.getvalue()


def derived_file() -> bytes:
    """Write a decoded file with spans, content types and roles, citing an input."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000, produced_by="pin 1",
                    produced_at=1_700_000_000) as writer:
        source = writer.add_source("zpf-input", uri="in.zpf", digest="sha256:00")
        decoder = writer.add_decoder("http/1.1", version="1")
        with writer.begin_session(proto="http", session_id=3) as session:
            part = session.participant("10.0.0.1:51000")
            cursor = 0
            for k in range(100):
                # One to three contiguous spans per record, so coverage holds.
                spans = []
                for _ in range(1 + k % 3):
                    spans.append(zpf.Span(source_id=source.source_id, session_id=7,
                                          participant_id=0, off_start=cursor,
                                          off_end=cursor + 3))
                    cursor += 3
                prim = k % 7 == 0
                session.record(
                    part,
                    ts=1000 + k,
                    payload=bytes([k]) if prim else f"field {k}".encode(),
                    decoded=zpf.Decoded(
                        decoder=decoder,
                        content_type="prim:u8" if prim else "mime:text/plain",
                        role="header" if k % 2 else None,
                        spans=tuple(spans),
                    ),
                )
    return sink.getvalue()


# Taken at `5aa1e67`, before #101 changed the write path.
CAPTURE_SHA256 = "33bfcb7b18fce5c94f996a968ddd0f72c8b5d2bd705423a39ba28200c547ceaa"
DERIVED_SHA256 = "4902fd84402fbd703c924fd9f32c49705ed85e09d282238c253321d3a5c05fa0"


def test_the_capture_files_bytes_have_not_moved():
    assert hashlib.sha256(capture_file()).hexdigest() == CAPTURE_SHA256


def test_the_derived_files_bytes_have_not_moved():
    assert hashlib.sha256(derived_file()).hexdigest() == DERIVED_SHA256


def test_both_pinned_files_are_conformant():
    """A pin of a file the reader refuses would pin the wrong thing."""
    for data in (capture_file(), derived_file()):
        with zpf.open(io.BytesIO(data)) as reader:
            for session in reader.sessions():
                list(session.records())
            assert reader.diagnostics == []

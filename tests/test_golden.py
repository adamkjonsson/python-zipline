"""Golden-file test: the 196-byte worked example from the specification.

The spec's "Worked example: a minimal raw file" gives a complete, annotated
hexdump of a conformant raw .zpf file. Writing the same five blocks must
produce those exact bytes, and parsing those bytes must yield the exact
field values.

The bytes are no longer transcribed here. Upstream ships that same example as
the ``raw-minimal`` conformance vector, built from the normative description
rather than copied from any implementation, so the vector *is* the golden
file — one fewer hand-maintained artifact to re-derive at each version bump.
"""

from __future__ import annotations

import io
import pathlib

import zpf

GOLDEN = (
    pathlib.Path(__file__).parent / "vectors/raw-minimal/raw-minimal.zpf"
).read_bytes()

GOLDEN_BLOCKS = [
    zpf.FileHeader(tick_hz=1_000_000),
    zpf.Source(source_id=1, kind=zpf.SourceKind.CAPTURE, uri="sideA.pcap"),
    zpf.Session(session_id=7, proto="tcp"),
    zpf.Participant(session_id=7, participant_id=0, endpoints=("10.0.0.1:51000",), isn=1000),
    zpf.Record(
        session_id=7,
        sender_pid=0,
        source_id=1,
        timestamp=1000,
        payload=b"GET / HTTP/1.1\r\n\r\n",
        flags=zpf.RecordFlags.PSH,
        seq_start=1001,
        ack=5001,
    ),
]


def test_golden_file_is_196_bytes():
    assert len(GOLDEN) == 196


def test_writing_the_blocks_produces_the_golden_bytes():
    """Our encoder reproduces the specification's own bytes exactly.

    Now that ``GOLDEN`` is the upstream vector, this is the strong form of
    the claim: the writer agrees byte-for-byte with a file built from the
    normative text by someone else, padding and option order included.
    """
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        for block in GOLDEN_BLOCKS:
            writer.write(block)
    assert sink.getvalue() == GOLDEN


def test_parsing_the_golden_bytes_yields_the_blocks():
    reader = zpf.BlockReader(io.BytesIO(GOLDEN))
    blocks = list(reader)
    assert blocks == GOLDEN_BLOCKS
    assert reader.header == GOLDEN_BLOCKS[0]
    assert not reader.complete  # no End block in the example
    assert not reader.truncated
    assert reader.diagnostics == []


def test_parsed_field_values_match_the_spec_annotations():
    header, source, session, participant, record = list(zpf.BlockReader(io.BytesIO(GOLDEN)))
    assert (header.version_major, header.version_minor) == (0, 16)
    assert header.tick_hz == 1_000_000
    assert (source.source_id, source.kind, source.uri) == (1, zpf.SourceKind.CAPTURE, "sideA.pcap")
    assert (session.session_id, session.proto) == (7, "tcp")
    assert not session.sequenced
    assert (participant.session_id, participant.participant_id) == (7, 0)
    assert participant.endpoint == "10.0.0.1:51000"
    assert participant.isn == 1000
    assert (record.session_id, record.sender_pid, record.source_id) == (7, 0, 1)
    assert record.timestamp == 1000
    assert record.payload == b"GET / HTTP/1.1\r\n\r\n"
    assert record.flags == zpf.RecordFlags.PSH
    assert (record.seq_start, record.ack) == (1001, 5001)
    # No decoder, so the layer rule makes this a transport stream: a byte run.
    assert record.decoder_id is None


def test_reparse_and_rewrite_is_byte_identical():
    blocks = list(zpf.BlockReader(io.BytesIO(GOLDEN)))
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        for block in blocks:
            writer.write(block)
    assert sink.getvalue() == GOLDEN


def test_the_worked_example_is_196_bytes():
    """The size the specification states for it, as a cheap sanity check."""
    assert len(GOLDEN) == 196

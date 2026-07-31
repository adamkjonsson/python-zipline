"""Golden-file test: the 196-byte worked example from the specification.

The spec's "Worked example: a minimal raw file" gives a complete, annotated
hexdump of a conformant raw .zpf file. Writing the same five blocks must
produce those exact bytes, and parsing those bytes must yield the exact
field values.
"""

from __future__ import annotations

import io
import pathlib

import zpf

GOLDEN = bytes.fromhex(
    # File Header (0x01)
    "01000000 10000000"
    "4650495a 00000c00"
    "40420f00 00000000"
    # Source Descriptor (0x02)
    "02000000 14000000"
    "01000000 20000a00"
    "73696465 412e7063 61700000"
    # Session Descriptor (0x10)
    "10000000 10000000"
    "07000000 00000000"
    "50000300 74637000"
    # Participant Descriptor (0x11)
    "11000000 28000000"
    "07000000 00000000"
    "00000000"
    "60000e00"
    "31302e30 2e302e31 3a353130 30300000"
    "61000400 e8030000"
    # Record (0x20)
    "20000000 40000000"
    "07000000 00000000"
    "00000100"
    "e8030000 00000000"
    "00000100"
    "12000000"
    "47455420 2f204854 54502f31 2e310d0a 0d0a0000"
    "70000400 e9030000"
    "72000400 89130000".replace(" ", "").replace("\n", "")
)

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
    assert (header.version_major, header.version_minor) == (0, 12)
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
    assert record.decoder_id is None  # a raw record, not a decoded one


def test_reparse_and_rewrite_is_byte_identical():
    blocks = list(zpf.BlockReader(io.BytesIO(GOLDEN)))
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        for block in blocks:
            writer.write(block)
    assert sink.getvalue() == GOLDEN


def test_golden_matches_the_upstream_vector():
    """The hand-written blob and the shipped vector are the same 196 bytes.

    Upstream builds ``raw-minimal`` from the specification's worked example,
    and this blob was transcribed from the same example independently. While
    both exist they must agree; when this blob is retired the vector is what
    remains.
    """
    vector = pathlib.Path(__file__).parent / "vectors/raw-minimal/raw-minimal.zpf"
    assert vector.read_bytes() == GOLDEN

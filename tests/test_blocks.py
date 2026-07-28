"""Unit tests for the typed block model: every registry option, preservation rules."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

import zpf
from zpf import _frame

FULL_BLOCKS = [
    zpf.FileHeader(
        tick_hz=1_000_000_000,
        time_epoch=-5,
        creator="test 1.0",
        produced_by="zpf-merge 1.2",
        produced_at=1_719_510_000,
        flags=zpf.FileFlags.SINGLE_CLOCK,
        comment="a header",
    ),
    zpf.Source(
        source_id=1,
        kind=zpf.SourceKind.CAPTURE,
        uri="sideA.pcap",
        digest="sha256:11aa",
        link_type=1,
        comment="a source",
    ),
    zpf.Decoder(
        decoder_id=2,
        name="http/1.1",
        version="0.4",
        params_digest="sha256:00ab",
        comment="a decoder",
    ),
    zpf.Session(
        session_id=2**63,
        proto="tcp",
        flow_key="10.0.0.1:51000 <-> 93.184.216.34:80",
        flags=zpf.SessionFlags.SEQUENCED,
        comment="a session",
    ),
    zpf.Participant(
        session_id=7,
        participant_id=0,
        endpoints=("vni:5001", "10.0.0.1:51000"),
        isn=0xFFFF_FFFF,
        identity="alice",
        tcp_role=zpf.TcpRole.INITIATOR,
        origin=zpf.Origin(source_id=1, session_id=3, participant_id=0),
        comment="a participant",
    ),
    zpf.SessionEnd(session_id=7, reason="fin", comment="an end"),
    zpf.Record(
        session_id=7,
        sender_pid=0,
        source_id=1,
        timestamp=-1000,
        payload=b"xyz",
        flags=zpf.RecordFlags.PSH | zpf.RecordFlags.FIN,
        seq_start=1001,
        ack=5001,
        ts_first=-2000,
        spans=(zpf.Span(source_id=1, session_id=7, participant_id=0, off_start=0, off_end=18),),
        decoder_id=2,
        content_type="dec:request",
        comment="a record",
    ),
    zpf.Undecoded(
        source_id=1,
        session_id=7,
        participant_id=1,
        off_start=100,
        off_end=139,
        reason="undecodable",
        decoder_id=2,
        comment="an undecoded",
    ),
    zpf.NameResolution(
        session_id=7, participant_id=0, label="alice", kind="nick", comment="a name"
    ),
    zpf.End(comment="bye"),
    zpf.Custom(pen=32473, subtype=7, payload=b"vend"),
    zpf.UnknownBlock(block_type=0x77, content=b"\x01\x02\x03\x04"),
]


@pytest.mark.parametrize("block", FULL_BLOCKS, ids=lambda b: type(b).__name__)
def test_every_block_round_trips_with_all_options(block: zpf.Block):
    content = block.to_bytes()
    assert len(content) % 4 == 0
    reparsed = zpf.parse_block(block.block_type, content)
    assert reparsed == block
    assert reparsed.to_bytes() == content


@pytest.mark.parametrize("payload_len", [0, 1, 2, 3, 4, 5, 17, 18])
def test_record_payload_padding(payload_len: int):
    record = zpf.Record(
        session_id=1, sender_pid=0, source_id=0, timestamp=0, payload=b"x" * payload_len
    )
    content = record.to_bytes()
    assert len(content) % 4 == 0
    assert zpf.Record.from_content(content).payload == b"x" * payload_len


def test_flag_conveniences():
    assert zpf.FileHeader(tick_hz=1, flags=zpf.FileFlags.SINGLE_CLOCK).single_clock
    assert not zpf.FileHeader(tick_hz=1).single_clock
    assert zpf.Session(session_id=1, flags=zpf.SessionFlags.SEQUENCED).sequenced
    assert not zpf.Session(session_id=1).sequenced


def test_zero_flags_options_are_omitted():
    assert _frame.OPT_FILE_FLAGS not in _option_ids(zpf.FileHeader(tick_hz=1))
    assert _frame.OPT_SESSION_FLAGS not in _option_ids(zpf.Session(session_id=1))


def _option_ids(block: zpf.Block) -> list[int]:
    body_size = 16 if isinstance(block, zpf.FileHeader) else 8
    region = block.to_bytes()[body_size:]
    return [opt.option_id for opt in _frame.iter_options(region)]


def test_endpoint_order_is_preserved():
    participant = zpf.Participant(
        session_id=1, participant_id=0, endpoints=("vni:5001", "gre:0x1234", "10.0.0.1:80")
    )
    reparsed = zpf.Participant.from_content(participant.to_bytes())
    assert reparsed.endpoints == ("vni:5001", "gre:0x1234", "10.0.0.1:80")
    assert reparsed.endpoint == "10.0.0.1:80"


def test_unknown_source_kind_is_kept_as_int():
    source = zpf.Source(source_id=1, kind=9)
    reparsed = zpf.Source.from_content(source.to_bytes())
    assert reparsed.kind == 9
    assert not isinstance(reparsed.kind, zpf.SourceKind)
    assert zpf.Source.from_content(zpf.Source(source_id=1, kind=1).to_bytes()).kind is (
        zpf.SourceKind.ZPF_INPUT
    )


def test_spans_auto_chunking_and_concatenation():
    many = tuple(
        zpf.Span(source_id=1, session_id=1, participant_id=0, off_start=i, off_end=i + 1)
        for i in range(_frame.MAX_SPANS_PER_OPTION + 5)
    )
    record = zpf.Record(session_id=1, sender_pid=0, source_id=1, timestamp=0, spans=many)
    content = record.to_bytes()
    spans_options = [
        opt
        for opt in _frame.iter_options(content[28:])
        if opt.option_id == _frame.OPT_SPANS
    ]
    assert len(spans_options) == 2
    assert len(spans_options[0].value) == _frame.MAX_SPANS_PER_OPTION * _frame.SPAN_ENTRY_SIZE
    assert zpf.Record.from_content(content).spans == many


def test_undecoded_body_is_byte_identical_to_a_span_entry():
    undecoded = zpf.Undecoded(
        source_id=1, session_id=7, participant_id=1, off_start=100, off_end=139
    )
    span = zpf.Span(source_id=1, session_id=7, participant_id=1, off_start=100, off_end=139)
    assert undecoded.to_bytes()[: _frame.SPAN_ENTRY_SIZE] == span.pack()


def test_unknown_option_is_preserved_and_byte_exact():
    session = zpf.Session(session_id=1, proto="tcp")
    content = session.to_bytes() + _frame.encode_option(0x0FFF, b"future!")
    reparsed = zpf.Session.from_content(content)
    assert reparsed.extra_options == (zpf.RawOption(0x0FFF, b"future!"),)
    assert reparsed.to_bytes() == content
    # A modified copy re-encodes canonically but keeps the unknown option.
    edited = dataclasses.replace(reparsed, comment="edited")
    assert zpf.Session.from_content(edited.to_bytes()).extra_options == reparsed.extra_options


def test_duplicate_single_valued_option_uses_first_and_preserves_rest():
    body = zpf.Session(session_id=1).to_bytes()
    content = (
        body
        + _frame.encode_option(_frame.OPT_PROTO, b"tcp")
        + _frame.encode_option(_frame.OPT_PROTO, b"udp")
    )
    reparsed = zpf.Session.from_content(content)
    assert reparsed.proto == "tcp"
    assert reparsed.extra_options == (zpf.RawOption(_frame.OPT_PROTO, b"udp"),)
    assert reparsed.to_bytes() == content


def test_malformed_known_option_is_preserved_raw():
    body = zpf.Participant(session_id=1, participant_id=0).to_bytes()
    content = body + _frame.encode_option(_frame.OPT_ISN, b"\x01\x02\x03")  # isn must be 4 bytes
    reparsed = zpf.Participant.from_content(content)
    assert reparsed.isn is None
    assert reparsed.extra_options == (zpf.RawOption(_frame.OPT_ISN, b"\x01\x02\x03"),)
    assert reparsed.to_bytes() == content


def test_reserved_record_flag_bits_survive_the_binary_face():
    # A writer MUST NOT set them and the JSONL face has no token for them, but
    # the binary face is byte-faithful: a reader must hand back what it read.
    # (The property generator sticks to the named bits, so this is their home.)
    record = zpf.Record(
        session_id=1, sender_pid=0, source_id=0, timestamp=0,
        flags=zpf.RecordFlags(0x2000) | zpf.RecordFlags.PSH,
    )
    reparsed = zpf.Record.from_content(record.to_bytes())
    assert int(reparsed.flags) == 0x2001
    assert reparsed == record
    assert reparsed.to_bytes() == record.to_bytes()


def test_unknown_tcp_role_value_is_preserved_raw():
    body = zpf.Participant(session_id=1, participant_id=0).to_bytes()
    content = body + _frame.encode_option(_frame.OPT_TCP_ROLE, b"\x09")
    reparsed = zpf.Participant.from_content(content)
    assert reparsed.tcp_role is None
    assert reparsed.extra_options == (zpf.RawOption(_frame.OPT_TCP_ROLE, b"\x09"),)


def test_replace_drops_the_byte_cache():
    original = zpf.Session.from_content(zpf.Session(session_id=1, proto="tcp").to_bytes())
    edited = dataclasses.replace(original, proto="udp")
    assert edited.proto == "udp"
    assert zpf.Session.from_content(edited.to_bytes()).proto == "udp"


@pytest.mark.parametrize(
    "build",
    [
        lambda: zpf.FileHeader(tick_hz=0),
        lambda: zpf.FileHeader(tick_hz=1, version_major=2),
        lambda: zpf.FileHeader(tick_hz=2**64),
        lambda: zpf.Source(source_id=2**16, kind=0),
        lambda: zpf.Session(session_id=2**64),
        lambda: zpf.Session(session_id=-1),
        lambda: zpf.Participant(session_id=1, participant_id=0, isn=2**32),
        lambda: zpf.Participant(session_id=1, participant_id=0, tcp_role=9),
        lambda: zpf.Record(session_id=1, sender_pid=0, source_id=0, timestamp=2**63),
        lambda: zpf.Record(session_id=1, sender_pid=0, source_id=0, timestamp=0, ack=-1),
        lambda: zpf.Span(source_id=1, session_id=1, participant_id=0, off_start=5, off_end=4),
        lambda: zpf.Custom(pen=1, subtype=1, payload=b"abc"),  # not a multiple of 4
        lambda: zpf.UnknownBlock(block_type=2**16, content=b""),
    ],
)
def test_invalid_values_raise_encode_error(build: Callable[[], object]):
    with pytest.raises(zpf.EncodeError):
        build()


def test_oversized_option_value_raises_at_serialization_time():
    decoder = zpf.Decoder(decoder_id=1, name="x" * 70_000)
    with pytest.raises(zpf.EncodeError):
        decoder.to_bytes()


def test_end_block_with_wrong_magic_is_semantically_invalid():
    with pytest.raises(zpf.SemanticError):
        zpf.End.from_content(b"\x00\x00\x00\x00")


def test_parse_block_dispatch():
    session = zpf.Session(session_id=5)
    parsed = zpf.parse_block(zpf.Session.block_type, session.to_bytes())
    assert parsed == session
    unknown = zpf.parse_block(0x55, b"\xde\xad\xbe\xef")
    assert unknown == zpf.UnknownBlock(block_type=0x55, content=b"\xde\xad\xbe\xef")


def test_short_body_is_structural():
    with pytest.raises(zpf.StructuralError):
        zpf.Session.from_content(b"\x01\x02")


def test_record_payload_len_overrun_is_structural():
    record = zpf.Record(session_id=1, sender_pid=0, source_id=0, timestamp=0, payload=b"abcd")
    content = bytearray(record.to_bytes())
    content[24:28] = (2**31).to_bytes(4, "little")  # forge payload_len
    with pytest.raises(zpf.StructuralError):
        zpf.Record.from_content(bytes(content))

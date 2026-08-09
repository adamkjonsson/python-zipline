"""Tests for the JSONL projection: spec examples, per-block mapping, edge policy."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING

import pytest

import zpf
from zpf import _frame
from zpf.jsonl import block_to_obj, dumps_block, loads_block, obj_to_block

if TYPE_CHECKING:
    from pathlib import Path

# --- The specification's own JSONL examples, used as golden input -------------

# "A first example": a 3-party chat room, dave joins mid-stream. Blank lines
# appear exactly as in the spec (they must be skipped).
CHAT_EXAMPLE = """\
{"type":"file","format":"zipline-payload/0.16","tick_hz":1000000}
{"type":"source","source_id":1,"kind":"capture","uri":"chat.pcap"}

{"type":"session","session_id":8,"proto":"irc","key":"#zipline@irc.example.net"}
{"type":"participant","session_id":8,"pid":0,"endpoint":["alice"]}
{"type":"participant","session_id":8,"pid":1,"endpoint":["bob"]}
{"type":"participant","session_id":8,"pid":2,"endpoint":["carol"]}

{"type":"record","session_id":8,"sender_pid":0,"source_id":1,"ts":2000,"payload":"aGksIGFsbCE="}
{"type":"record","session_id":8,"sender_pid":2,"source_id":1,"ts":2100,"payload":"aGV5IGFsaWNl"}
{"type":"record","session_id":8,"sender_pid":1,"source_id":1,"ts":2150,"payload":"bW9ybmluZw=="}

{"type":"participant","session_id":8,"pid":3,"endpoint":["dave"]}
{"type":"record","session_id":8,"sender_pid":3,"source_id":1,"ts":2300,"payload":"YW0gSSBsYXRlPw=="}

{"type":"session_end","session_id":8,"reason":"timeout"}
"""

# "Worked example: a skewed two-file capture".
SKEWED_EXAMPLE = """\
{"type":"file","format":"zipline-payload/0.16","tick_hz":1000000}
{"type":"source","source_id":1,"kind":"capture","uri":"sideA.pcap"}
{"type":"source","source_id":2,"kind":"capture","uri":"sideB.pcap"}
{"type":"session","session_id":7,"proto":"tcp","key":"10.0.0.1:51000 <-> 93.184.216.34:80"}
{"type":"participant","session_id":7,"pid":0,"endpoint":["10.0.0.1:51000"],"isn":1000}
{"type":"participant","session_id":7,"pid":1,"endpoint":["93.184.216.34:80"],"isn":5000}
{"type":"record","session_id":7,"sender_pid":0,"source_id":1,"ts":1000,"seq_start":1001,"ack":5001,"payload":"R0VUIC8gSFRUUC8xLjENCg0K"}
{"type":"record","session_id":7,"sender_pid":1,"source_id":2,"ts":995,"seq_start":5001,"ack":1019,"payload":"SFRUUC8xLjEgMjAwIE9LDQouLi4="}
"""

# The merged pass-through file derived from the skewed capture.
MERGED_EXAMPLE = "\n".join(
    [
        '{"type":"file","format":"zipline-payload/0.16","tick_hz":1000000,'
        '"produced_by":"zpf-merge 1.2","produced_at":1719510000}',
        '{"type":"source","source_id":1,"kind":"zpf-input","uri":"sideA.zpf","digest":"sha256:11aa…"}',
        '{"type":"source","source_id":2,"kind":"zpf-input","uri":"sideB.zpf","digest":"sha256:22bb…"}',
        '{"type":"session","session_id":1,"proto":"tcp",'
        '"key":"10.0.0.1:51000 <-> 93.184.216.34:80","sequenced":true}',
        '{"type":"participant","session_id":1,"pid":0,"endpoint":["10.0.0.1:51000"],"isn":1000,'
        '"origin":{"source_id":1,"session_id":7,"pid":0}}',
        '{"type":"participant","session_id":1,"pid":1,"endpoint":["93.184.216.34:80"],"isn":5000,'
        '"origin":{"source_id":2,"session_id":3,"pid":0}}',
        '{"type":"record","session_id":1,"sender_pid":0,"source_id":1,"ts":1000,'
        '"seq_start":1001,"ack":5001,"payload":"R0VUIC8gSFRUUC8xLjENCg0K"}',
        '{"type":"record","session_id":1,"sender_pid":1,"source_id":2,"ts":995,'
        '"seq_start":5001,"ack":1019,"payload":"SFRUUC8xLjEgMjAwIE9LDQouLi4="}',
        "",
    ]
)

# "A decoded file, end to end" (payload placeholders replaced with real base64).
DECODED_EXAMPLE = "\n".join(
    [
        '{"type":"file","format":"zipline-payload/0.16","tick_hz":1000000,'
        '"produced_by":"zpf-decode 0.4","produced_at":1719500000}',
        '{"type":"source","source_id":1,"kind":"zpf-input","uri":"raw.zpf","digest":"sha256:9f2c…"}',
        '{"type":"decoder","decoder_id":1,"output_layer":"decoded","name":"http/1.1",'
        '"version":"0.4","params_digest":"sha256:00ab…"}',
        '{"type":"session","session_id":7,"proto":"http"}',
        '{"type":"participant","session_id":7,"pid":0,"endpoint":["10.0.0.1:51000"]}',
        '{"type":"participant","session_id":7,"pid":1,"endpoint":["93.184.216.34:80"]}',
        '{"type":"record","session_id":7,"sender_pid":0,"ts":1000,"decoder_id":1,"source_id":1,'
        '"spans":[{"source_id":1,"session_id":7,"pid":0,"off_start":0,"off_end":18}],'
        '"content_type":"dec:request","payload":"cmVxdWVzdA=="}',
        '{"type":"record","session_id":7,"sender_pid":1,"ts":995,"decoder_id":1,"source_id":1,'
        '"spans":[{"source_id":1,"session_id":7,"pid":1,"off_start":0,"off_end":100}],'
        '"content_type":"dec:response","payload":"cmVzcG9uc2U="}',
        '{"type":"undecoded","source_id":1,"session_id":7,"pid":1,'
        '"off_start":100,"off_end":139,"reason":"undecodable","decoder_id":1}',
        "",
    ]
)

SPEC_EXAMPLES = [CHAT_EXAMPLE, SKEWED_EXAMPLE, MERGED_EXAMPLE, DECODED_EXAMPLE]


def read_all(text: str, **kwargs: bool) -> tuple[list[zpf.Block], zpf.JsonlReader]:
    reader = zpf.JsonlReader(io.StringIO(text), **kwargs)
    return list(reader), reader


def write_all(blocks: list[zpf.Block]) -> str:
    sink = io.StringIO()
    with zpf.JsonlWriter(sink) as writer:
        for block in blocks:
            writer.write(block)
    return sink.getvalue()


def test_chat_example_parses_to_typed_blocks():
    blocks, reader = read_all(CHAT_EXAMPLE)
    assert reader.header == zpf.FileHeader(tick_hz=1_000_000)
    assert not reader.truncated
    assert reader.diagnostics == []
    session = blocks[2]
    assert session == zpf.Session(session_id=8, proto="irc", flow_key="#zipline@irc.example.net")
    assert not session.sequenced
    dave = blocks[9]
    assert dave == zpf.Participant(session_id=8, participant_id=3, endpoints=("dave",))
    assert blocks[10].payload == b"am I late?"
    assert blocks[-1] == zpf.SessionEnd(session_id=8, reason="timeout")


def test_skewed_example_parses_ordering_hints():
    blocks, _ = read_all(SKEWED_EXAMPLE)
    client, server = blocks[4], blocks[5]
    assert (client.isn, server.isn) == (1000, 5000)
    request, response = blocks[6], blocks[7]
    assert (request.seq_start, request.ack) == (1001, 5001)
    assert (response.seq_start, response.ack) == (5001, 1019)
    assert response.timestamp == 995  # skewed: earlier than the request it answers
    assert request.payload == b"GET / HTTP/1.1\r\n\r\n"


def test_merged_example_parses_provenance():
    blocks, _ = read_all(MERGED_EXAMPLE)
    header = blocks[0]
    assert (header.produced_by, header.produced_at) == ("zpf-merge 1.2", 1_719_510_000)
    assert blocks[1].kind is zpf.SourceKind.ZPF_INPUT
    assert blocks[3].sequenced
    assert blocks[4].origin == zpf.Origin(source_id=1, session_id=7, participant_id=0)
    assert blocks[5].origin == zpf.Origin(source_id=2, session_id=3, participant_id=0)


def test_decoded_example_parses_decode_stage():
    blocks, _ = read_all(DECODED_EXAMPLE)
    assert blocks[2] == zpf.Decoder(
        decoder_id=1, name="http/1.1", version="0.4", params_digest="sha256:00ab…"
    )
    request = blocks[6]
    assert request.decoder_id == 1
    assert request.content_type == "dec:request"
    assert request.spans == (
        zpf.Span(source_id=1, session_id=7, participant_id=0, off_start=0, off_end=18),
    )
    assert blocks[8] == zpf.Undecoded(
        source_id=1,
        session_id=7,
        participant_id=1,
        off_start=100,
        off_end=139,
        reason="undecodable",
        decoder_id=1,
    )


@pytest.mark.parametrize("example", SPEC_EXAMPLES, ids=["chat", "skewed", "merged", "decoded"])
def test_spec_examples_reemit_to_the_same_objects(example: str):
    blocks, _ = read_all(example)
    emitted = write_all(blocks)
    original_objs = [json.loads(line) for line in example.splitlines() if line.strip()]
    emitted_objs = [json.loads(line) for line in emitted.splitlines()]
    assert emitted_objs == original_objs


# --- Per-block projection ------------------------------------------------------

FULL_BLOCKS = [
    zpf.FileHeader(
        tick_hz=1_000_000_000,
        time_epoch=-5,
        creator="test 1.0",
        produced_by="zpf-merge 1.2",
        produced_at=1_719_510_000,
        flags=zpf.FileFlags.SINGLE_CLOCK,
        comment="a header",
        extra_options=(zpf.RawOption(0x0FFF, b"x"),),
    ),
    zpf.Source(
        source_id=1, kind=zpf.SourceKind.CAPTURE, uri="a.pcap", digest="sha256:11", link_type=1
    ),
    zpf.Decoder(decoder_id=2, name="http/1.1", version="0.4", params_digest="sha256:ab"),
    zpf.Session(
        session_id=2**63,
        proto="tcp",
        flow_key="a <-> b",
        flags=zpf.SessionFlags.SEQUENCED,
        sequenced_basis="clock",
        comment="s",
    ),
    zpf.Participant(
        session_id=7,
        participant_id=0,
        endpoints=("vni:5001", "10.0.0.1:51000"),
        isn=0xFFFF_FFFF,
        identity="alice",
        tcp_role=zpf.TcpRole.RESPONDER,
        origin=zpf.Origin(source_id=1, session_id=3, participant_id=0),
    ),
    zpf.SessionEnd(session_id=7, reason="fin", comment="bye"),
    zpf.Record(
        session_id=7,
        sender_pid=0,
        source_id=1,
        timestamp=-(2**60),
        payload=b"",
        flags=zpf.RecordFlags.SYN | zpf.RecordFlags.MESSAGE,
        seq_start=1001,
        ack=5001,
        ts_first=-(2**60) - 5,
        spans=(zpf.Span(source_id=1, session_id=7, participant_id=0, off_start=0, off_end=2**60),),
        decoder_id=2,
        content_type="prim:bytes",
        extra_options=(zpf.RawOption(0x0F00, b"zz"),),
    ),
    zpf.Undecoded(
        source_id=1,
        session_id=7,
        participant_id=1,
        off_start=100,
        off_end=139,
        reason="rtp-seq-gap",
        reason_class="hole",
    ),
    zpf.NameResolution(session_id=7, participant_id=0, label="alice", kind="nick"),
    zpf.End(comment="done"),
    zpf.Custom(pen=32473, subtype=7, payload=b"vend"),
    zpf.UnknownBlock(block_type=0x77, content=b"\x01\x02\x03\x04"),
]


@pytest.mark.parametrize("block", FULL_BLOCKS, ids=lambda b: type(b).__name__)
def test_every_block_round_trips_through_jsonl(block: zpf.Block):
    assert loads_block(dumps_block(block)) == block


def test_aliases_and_key_shapes():
    obj = block_to_obj(FULL_BLOCKS[6])  # the Record
    assert obj["ts"] == str(-(2**60))  # alias + decimal-string rule
    assert obj["flags"] == ["syn", "message"]
    assert obj["payload"] == ""  # body field: present even when empty
    assert obj["spans"][0]["off_end"] == str(2**60)
    session_obj = block_to_obj(FULL_BLOCKS[3])
    assert session_obj["session_id"] == str(2**63)
    assert session_obj["key"] == "a <-> b"
    assert session_obj["sequenced"] is True
    participant_obj = block_to_obj(FULL_BLOCKS[4])
    assert participant_obj["pid"] == 0
    assert participant_obj["endpoint"] == ["vni:5001", "10.0.0.1:51000"]  # tunnel = array
    assert participant_obj["tcp_role"] == "responder"
    header_obj = block_to_obj(FULL_BLOCKS[0])
    assert header_obj["single_clock"] is True
    assert header_obj["options"] == [{"id": "0x0FFF", "value": "eA=="}]


def test_zero_flags_and_absent_options_are_omitted():
    obj = block_to_obj(zpf.Record(session_id=1, sender_pid=0, source_id=0, timestamp=0))
    assert set(obj) == {"type", "session_id", "sender_pid", "source_id", "ts", "payload"}
    assert "sequenced" not in block_to_obj(zpf.Session(session_id=1))
    assert "single_clock" not in block_to_obj(zpf.FileHeader(tick_hz=1))


def test_tcp_role_unknown_is_omitted_like_absent():
    explicit = zpf.Participant(session_id=1, participant_id=0, tcp_role=zpf.TcpRole.UNKNOWN)
    obj = block_to_obj(explicit)
    assert "tcp_role" not in obj
    assert obj_to_block(obj).tcp_role is None  # the spec-mandated normalization


@pytest.mark.parametrize("tick_hz", [1, 10**3, 10**6, 10**9, 44_100])
def test_tick_hz_is_a_rate_never_a_unit_label(tick_hz: int):
    """The key carries the binary field's number, not ``"us"``/``"ms"``."""
    obj = block_to_obj(zpf.FileHeader(tick_hz=tick_hz))
    assert obj["tick_hz"] == tick_hz
    assert "time_units" not in obj
    assert obj_to_block(obj).tick_hz == tick_hz


def test_tick_hz_accepts_decimal_strings():
    header = obj_to_block(
        {"type": "file", "format": "zipline-payload/0.16", "tick_hz": str(2**60)}
    )
    assert header.tick_hz == 2**60


def test_time_units_is_no_longer_accepted():
    """0.10 removed the key outright rather than deprecating it."""
    with pytest.raises(ValueError, match="tick_hz"):
        obj_to_block({"type": "file", "format": "zipline-payload/0.16", "time_units": "us"})


def test_format_string_round_trips_the_supported_version():
    header = zpf.FileHeader(tick_hz=1)
    obj = block_to_obj(header)
    assert obj["format"] == "zipline-payload/0.16"
    assert obj_to_block(obj) == header


def test_64_bit_fields_accept_both_number_and_string():
    for value in (2000, "2000"):
        record = obj_to_block(
            {
                "type": "record",
                "session_id": value,
                "sender_pid": 0,
                "source_id": 1,
                "ts": value,
                "payload": "",
            }
        )
        assert (record.session_id, record.timestamp) == (2000, 2000)


def test_duplicate_single_valued_options_survive_via_options_array():
    content = (
        zpf.Session(session_id=1).to_bytes()
        + _frame.encode_option(_frame.OPT_PROTO, b"tcp")
        + _frame.encode_option(_frame.OPT_PROTO, b"udp")
    )
    session = zpf.Session.from_content(content)
    round_tripped = loads_block(dumps_block(session))
    assert round_tripped.proto == "tcp"
    assert round_tripped.extra_options == (zpf.RawOption(_frame.OPT_PROTO, b"udp"),)


# --- Edge policy ---------------------------------------------------------------


def test_unknown_jsonl_key_is_dropped_with_issue():
    issues: list[str] = []
    block = obj_to_block(
        {"type": "session", "session_id": 1, "wat": 42}, on_issue=issues.append
    )
    assert block == zpf.Session(session_id=1)
    assert issues and "wat" in issues[0]


def test_unknown_jsonl_key_reader_policy():
    text = CHAT_EXAMPLE.replace(
        '{"type":"session_end","session_id":8,"reason":"timeout"}',
        '{"type":"session_end","session_id":8,"reason":"timeout","wat":1}',
    )
    blocks, reader = read_all(text)
    assert blocks[-1] == zpf.SessionEnd(session_id=8, reason="timeout")
    assert reader.diagnostics[0].category == "jsonl-edge"
    with pytest.raises(zpf.SemanticError):
        read_all(text, strict=True)


def test_reserved_record_flag_bits_escape_as_hex_tokens():
    """A bit with no token is preserved, not dropped — one token per bit."""
    record = zpf.Record(
        session_id=1, sender_pid=0, source_id=0, timestamp=0, flags=zpf.RecordFlags(0x2001)
    )
    issues: list[str] = []
    obj = block_to_obj(record, on_issue=issues.append)
    assert obj["flags"] == ["psh", "0x2000"]
    assert issues == []  # Preserving is the conformant path, not an edge case.
    assert obj_to_block(obj).flags == record.flags


def test_several_reserved_flag_bits_each_get_their_own_token():
    record = zpf.Record(
        session_id=1, sender_pid=0, source_id=0, timestamp=0, flags=zpf.RecordFlags(0x0120)
    )
    obj = block_to_obj(record)
    assert obj["flags"] == ["0x0020", "0x0100"]
    assert obj_to_block(obj).flags == record.flags


def test_unknown_flag_token_is_dropped_on_read():
    issues: list[str] = []
    record = obj_to_block(
        {
            "type": "record",
            "session_id": 1,
            "sender_pid": 0,
            "source_id": 0,
            "ts": 0,
            "flags": ["psh", "zap"],
            "payload": "",
        },
        on_issue=issues.append,
    )
    assert record.flags == zpf.RecordFlags.PSH
    assert issues and "zap" in issues[0]


def test_numeric_source_kind_round_trips():
    source = zpf.Source(source_id=1, kind=9)
    obj = block_to_obj(source)
    assert obj["kind"] == 9
    assert loads_block(dumps_block(source)) == source


@pytest.mark.parametrize(
    ("layer", "label"),
    [(zpf.OutputLayer.DECODED, "decoded"), (zpf.OutputLayer.TRANSPORT, "transport")],
)
def test_output_layer_renders_as_its_label_both_ways(layer: zpf.OutputLayer, label: str):
    decoder = zpf.Decoder(decoder_id=1, output_layer=layer, name="x")
    assert block_to_obj(decoder)["output_layer"] == label
    assert loads_block(dumps_block(decoder)) == decoder


def test_output_layer_is_always_written():
    """A body field has no absent case, so the line always carries it.

    That is the difference from an option, and it is why a reader may take
    the key as given rather than defining what its absence would mean.
    """
    assert block_to_obj(zpf.Decoder(decoder_id=1))["output_layer"] == "decoded"


def test_a_decoder_line_without_an_output_layer_is_rejected():
    line = '{"type":"decoder","decoder_id":1,"name":"http/1.1"}'
    with pytest.raises(ValueError, match="output_layer"):
        loads_block(line)


def test_numeric_output_layer_round_trips():
    """Load-bearing, so the raw number survives and is not resolved.

    Rendering it as a number rather than dropping the key is what keeps the
    escape honest: a consumer sees a value it cannot act on, which is the
    true statement, instead of a plausible label or a silent default.
    """
    decoder = zpf.Decoder(decoder_id=1, output_layer=9)
    obj = block_to_obj(decoder)
    assert obj["output_layer"] == 9
    assert loads_block(dumps_block(decoder)) == decoder


def test_unknown_binary_block_escapes_as_a_hex_type():
    """The type itself carries the number; the line has no other key."""
    unknown = zpf.UnknownBlock(block_type=0x77, content=b"\x01\x02\x03\x04")
    obj = block_to_obj(unknown)
    assert obj == {"type": "0x0077", "content": "AQIDBA=="}
    assert obj_to_block(obj) == unknown


def test_unknown_type_string_line_is_isolated():
    text = CHAT_EXAMPLE + '{"type":"fancy","x":1}\n'
    blocks, reader = read_all(text)
    assert len(blocks) == 12
    assert reader.diagnostics[0].category == "invalid-line"
    with pytest.raises(zpf.SemanticError):
        read_all(text, strict=True)


# --- Reader / writer stream semantics -------------------------------------------


def test_reader_requires_a_file_line_first():
    with pytest.raises(zpf.StructuralError):
        read_all('{"type":"session","session_id":1}\n')
    with pytest.raises(zpf.StructuralError):
        read_all("")


def test_reader_rejects_second_file_line():
    line = '{"type":"file","format":"zipline-payload/0.16","tick_hz":1000000}\n'
    with pytest.raises(zpf.StructuralError):
        read_all(line + line)


def test_reader_rejects_unsupported_major_version():
    with pytest.raises(zpf.StructuralError):
        read_all('{"type":"file","format":"zipline-payload/2","tick_hz":1000000}\n')


def test_reader_rejects_an_unimplemented_minor():
    """While the major is 0, an unknown minor is structural corruption."""
    with pytest.raises(zpf.StructuralError):
        read_all('{"type":"file","format":"zipline-payload/0.11","tick_hz":1000000}\n')


def test_format_components_are_compared_separately():
    """0.9 is *older* than 0.14: a float parse would sort it as newer."""
    with pytest.raises(zpf.StructuralError):
        read_all('{"type":"file","format":"zipline-payload/0.9","tick_hz":1000000}\n')


def test_truncated_final_line():
    text = SKEWED_EXAMPLE[:-30]  # cut inside the last record line
    blocks, reader = read_all(text)
    assert len(blocks) == 7
    assert reader.truncated
    assert not reader.complete
    with pytest.raises(zpf.TruncatedError):
        read_all(text, strict=True)


def test_invalid_json_mid_file_is_structural():
    lines = CHAT_EXAMPLE.splitlines()
    lines[3] = '{"type":'  # not the final line
    with pytest.raises(zpf.StructuralError):
        read_all("\n".join(lines))


def test_content_after_end_line():
    text = write_all([zpf.FileHeader(tick_hz=1), zpf.End()]) + '{"type":"session"}\n'
    blocks, reader = read_all(text)
    assert blocks == [zpf.FileHeader(tick_hz=1), zpf.End()]
    assert reader.complete
    assert reader.diagnostics[0].category == "trailing-lines"
    with pytest.raises(zpf.SemanticError):
        read_all(text, strict=True)


def test_writer_structural_rules():
    with zpf.JsonlWriter(io.StringIO()) as writer:
        with pytest.raises(zpf.StructuralError):
            writer.write(zpf.Session(session_id=1))
        assert writer.write(zpf.FileHeader(tick_hz=1)) == 1
        with pytest.raises(zpf.StructuralError):
            writer.write(zpf.FileHeader(tick_hz=1))
        assert writer.write(zpf.End()) == 2
        with pytest.raises(zpf.StructuralError):
            writer.write(zpf.Session(session_id=1))
    with pytest.raises(zpf.ZpfError):
        writer.write(zpf.Session(session_id=1))


def test_jsonl_path_round_trip(tmp_path: Path):
    path = tmp_path / "cap.zpf.jsonl"
    blocks = [zpf.FileHeader(tick_hz=1_000_000), zpf.Session(session_id=1), zpf.End()]
    with zpf.JsonlWriter(path) as writer:
        for block in blocks:
            writer.write(block)
    with zpf.JsonlReader(path) as reader:
        assert list(reader) == blocks
        assert reader.complete


# --- Whole-file converters -------------------------------------------------------


def test_binary_jsonl_binary_is_semantically_lossless():
    binary = io.BytesIO()
    with zpf.BlockWriter(binary) as writer:
        for block in FULL_BLOCKS[:-2]:  # Custom/Unknown covered separately below
            writer.write(block)
    text = io.StringIO()
    zpf.binary_to_jsonl(io.BytesIO(binary.getvalue()), text)
    binary_again = io.BytesIO()
    text.seek(0)
    zpf.jsonl_to_binary(text, binary_again)
    original = list(zpf.BlockReader(io.BytesIO(binary.getvalue())))
    converted = list(zpf.BlockReader(io.BytesIO(binary_again.getvalue())))
    assert converted == original


def test_converter_carries_unknown_blocks():
    binary = io.BytesIO()
    with zpf.BlockWriter(binary) as writer:
        writer.write(zpf.FileHeader(tick_hz=1))
        writer.write(zpf.UnknownBlock(block_type=0x0F42, content=b"\xde\xad\xbe\xef"))
        writer.write(zpf.Custom(pen=1, subtype=2, payload=b"abcd"))
    text = io.StringIO()
    zpf.binary_to_jsonl(io.BytesIO(binary.getvalue()), text)
    lines = text.getvalue().splitlines()
    assert json.loads(lines[1])["type"] == "0x0F42"
    assert json.loads(lines[2])["type"] == "custom"
    binary_again = io.BytesIO()
    text.seek(0)
    zpf.jsonl_to_binary(text, binary_again)
    assert binary_again.getvalue() == binary.getvalue()  # canonical in, canonical out


# --- 0.13/0.14 projections ---------------------------------------------------


def test_discontinuity_projects_and_round_trips():
    block = zpf.Discontinuity(session_id=9, participant_id=0, width=25, reason="stream-gap")
    obj = block_to_obj(block)
    assert obj == {
        "type": "discontinuity",
        "session_id": 9,
        "pid": 0,
        "width": 25,
        "reason": "stream-gap",
    }
    assert loads_block(dumps_block(block)) == block


def test_an_unknown_discontinuity_width_is_an_omitted_key():
    """Absent must not project as ``0``, or the two declarations merge.

    The binary form keeps them apart by omitting the option; the projection
    keeps them apart by omitting the key. A reader treats a missing key as
    "option not present", never as a present option carrying a default.
    """
    unknown = zpf.Discontinuity(session_id=7, participant_id=0, reason="tls-record-lost")
    zero = zpf.Discontinuity(session_id=7, participant_id=0, width=0, reason="tls-record-lost")
    assert "width" not in block_to_obj(unknown)
    assert block_to_obj(zero)["width"] == 0
    assert loads_block(dumps_block(unknown)).width is None
    assert loads_block(dumps_block(zero)).width == 0


def test_input_extents_project_as_an_array_even_when_single():
    end = zpf.SessionEnd(
        session_id=7,
        reason="fin",
        input_extents=(
            zpf.InputExtent(source_id=1, session_id=7, participant_id=0, extent=18),
        ),
    )
    obj = block_to_obj(end)
    assert obj["input_extents"] == [
        {"source_id": 1, "session_id": 7, "pid": 0, "extent": 18}
    ]
    assert loads_block(dumps_block(end)) == end


def test_input_extent_chunking_is_invisible_to_the_projection():
    """Several binary occurrences merge into one array, and may re-split.

    The chunking exists because a TLV value caps at 65 535 bytes; it carries
    no meaning, so the projection must not expose it.
    """
    many = tuple(
        zpf.InputExtent(source_id=1, session_id=1, participant_id=0, extent=i)
        for i in range(_frame.MAX_EXTENTS_PER_OPTION + 5)
    )
    end = zpf.SessionEnd(session_id=1, input_extents=many)
    obj = block_to_obj(end)
    assert len(obj["input_extents"]) == len(many)
    rebuilt = loads_block(dumps_block(end))
    assert rebuilt.input_extents == many
    options = [
        opt
        for opt in _frame.iter_options(rebuilt.to_bytes()[8:])
        if opt.option_id == _frame.OPT_INPUT_EXTENTS
    ]
    assert len(options) == 2


def test_external_session_id_projects_as_base64():
    """It is opaque bytes, so it must not be spelled out as text.

    This value is printable ASCII, which is exactly when a converter is
    tempted to write the string — and then the same id has two spellings.
    """
    session = zpf.Session(session_id=7, proto="tcp", external_session_id=b"case-12345678901")
    obj = block_to_obj(session)
    assert obj["external_session_id"] == "Y2FzZS0xMjM0NTY3ODkwMQ=="
    rebuilt = loads_block(dumps_block(session))
    assert rebuilt.external_session_id == b"case-12345678901"
    assert rebuilt == session


def test_transform_params_digest_projects_on_the_file_line():
    header = zpf.FileHeader(tick_hz=1000, transform_params_digest="sha256:7c1e")
    obj = block_to_obj(header)
    assert obj["transform_params_digest"] == "sha256:7c1e"
    assert loads_block(dumps_block(header)) == header


def test_width_and_extent_accept_a_decimal_string():
    """Both joined the 64-bit set, so both MUST be readable either way."""
    big = 2**60
    disc = loads_block(
        json.dumps({"type": "discontinuity", "session_id": "7", "pid": 0, "width": str(big)})
    )
    assert disc.width == big
    end = loads_block(
        json.dumps(
            {
                "type": "session_end",
                "session_id": 7,
                "input_extents": [
                    {"source_id": 1, "session_id": "7", "pid": 0, "extent": str(big)}
                ],
            }
        )
    )
    assert end.input_extents[0].extent == big


def test_a_wide_width_projects_as_a_decimal_string():
    block = zpf.Discontinuity(session_id=7, participant_id=0, width=2**60)
    assert block_to_obj(block)["width"] == str(2**60)

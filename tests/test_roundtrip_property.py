"""Property tests: random files round-trip; arbitrary bytes never crash the reader."""

from __future__ import annotations

import contextlib
import dataclasses
import io

from hypothesis import given
from hypothesis import strategies as st

import zpf

ZpfFile = tuple[zpf.FileHeader, list[zpf.Block], bool]

u16 = st.integers(0, 2**16 - 1)
u32 = st.integers(0, 2**32 - 1)
u64 = st.integers(0, 2**64 - 1)
i64 = st.integers(-(2**63), 2**63 - 1)
text = st.text(max_size=30)
opt_text = st.none() | text
payloads = st.binary(max_size=64)

# Unknown-to-the-registry option ids, so values stay opaque and preserved.
extra_options = st.lists(
    st.builds(zpf.RawOption, st.integers(0x0F00, 0x0FFF), st.binary(max_size=16)),
    max_size=3,
).map(tuple)

spans = st.lists(
    st.builds(
        lambda source_id, session_id, pid, start, length: zpf.Span(
            source_id=source_id,
            session_id=session_id,
            participant_id=pid,
            off_start=start,
            off_end=start + length,
        ),
        u16,
        u64,
        u16,
        st.integers(0, 2**32),
        st.integers(0, 2**16),
    ),
    max_size=4,
).map(tuple)

file_headers = st.builds(
    zpf.FileHeader,
    tick_hz=st.integers(1, 2**64 - 1),
    time_epoch=st.none() | i64,
    creator=opt_text,
    produced_by=opt_text,
    produced_at=st.none() | i64,
    flags=st.sampled_from([zpf.FileFlags(0), zpf.FileFlags.SINGLE_CLOCK]),
    comment=opt_text,
    extra_options=extra_options,
)

sources = st.builds(
    zpf.Source,
    source_id=u16,
    kind=st.sampled_from(list(zpf.SourceKind)) | st.integers(0, 255),
    uri=opt_text,
    digest=opt_text,
    link_type=st.none() | u16,
    comment=opt_text,
    extra_options=extra_options,
)

decoders = st.builds(
    zpf.Decoder,
    decoder_id=u16,
    name=opt_text,
    version=opt_text,
    params_digest=opt_text,
    comment=opt_text,
    extra_options=extra_options,
)

sessions = st.builds(
    zpf.Session,
    session_id=u64,
    proto=opt_text,
    flow_key=opt_text,
    flags=st.sampled_from([zpf.SessionFlags(0), zpf.SessionFlags.SEQUENCED]),
    comment=opt_text,
    extra_options=extra_options,
)

origins = st.builds(zpf.Origin, source_id=u16, session_id=u64, participant_id=u16)

participants = st.builds(
    zpf.Participant,
    session_id=u64,
    participant_id=u16,
    endpoints=st.lists(text, max_size=3).map(tuple),
    isn=st.none() | u32,
    identity=opt_text,
    tcp_role=st.none() | st.sampled_from(list(zpf.TcpRole)),
    origin=st.none() | origins,
    comment=opt_text,
    extra_options=extra_options,
)

session_ends = st.builds(
    zpf.SessionEnd, session_id=u64, reason=opt_text, comment=opt_text, extra_options=extra_options
)

# The flag bits the format names. The rest are reserved and MUST be 0, so no
# conformant writer emits them and the JSONL face has no token to render them
# with — it drops them with a diagnostic (owned by
# test_jsonl.py::test_unrenderable_record_flag_bits, and by the binary
# preservation test in test_blocks.py). Generating them here would assert
# projection stability for files that cannot legally exist.
_DEFINED_RECORD_FLAGS = (
    zpf.RecordFlags.PSH
    | zpf.RecordFlags.FIN
    | zpf.RecordFlags.RST
    | zpf.RecordFlags.SYN
    | zpf.RecordFlags.URG
    | zpf.RecordFlags.RETRANSMIT
    | zpf.RecordFlags.MESSAGE
)

records = st.builds(
    zpf.Record,
    session_id=u64,
    sender_pid=u16,
    source_id=u16,
    timestamp=i64,
    payload=payloads,
    flags=st.integers(0, 2**16 - 1).map(lambda bits: zpf.RecordFlags(bits) & _DEFINED_RECORD_FLAGS),
    seq_start=st.none() | u32,
    ack=st.none() | u32,
    ts_first=st.none() | i64,
    spans=spans,
    decoder_id=st.none() | u16,
    content_type=opt_text,
    comment=opt_text,
    extra_options=extra_options,
)

undecodeds = st.builds(
    lambda source_id, session_id, pid, start, length, reason, decoder_id, comment, extras: (
        zpf.Undecoded(
            source_id=source_id,
            session_id=session_id,
            participant_id=pid,
            off_start=start,
            off_end=start + length,
            reason=reason,
            decoder_id=decoder_id,
            comment=comment,
            extra_options=extras,
        )
    ),
    u16,
    u64,
    u16,
    st.integers(0, 2**32),
    st.integers(0, 2**16),
    opt_text,
    st.none() | u16,
    opt_text,
    extra_options,
)

names = st.builds(
    zpf.NameResolution,
    session_id=u64,
    participant_id=u16,
    label=opt_text,
    kind=opt_text,
    comment=opt_text,
    extra_options=extra_options,
)

customs = st.builds(
    zpf.Custom,
    pen=u32,
    subtype=u16,
    payload=st.binary(max_size=16).map(lambda b: b[: len(b) - len(b) % 4]),
)

unknowns = st.builds(
    zpf.UnknownBlock,
    block_type=st.integers(0x0100, 0xFFFE),
    content=st.binary(max_size=32).map(lambda b: b[: len(b) - len(b) % 4]),
)

body_blocks = st.lists(
    st.one_of(
        sources,
        decoders,
        sessions,
        participants,
        session_ends,
        records,
        undecodeds,
        names,
        customs,
        unknowns,
    ),
    max_size=8,
)

files = st.tuples(file_headers, body_blocks, st.booleans())


def write_file(header: zpf.FileHeader, body: list[zpf.Block], with_end: bool) -> bytes:
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        writer.write(header)
        for block in body:
            writer.write(block)
        if with_end:
            writer.write(zpf.End())
    return sink.getvalue()


@given(files)
def test_write_read_round_trip(file: ZpfFile):
    header, body, with_end = file
    expected = [header, *body] + ([zpf.End()] if with_end else [])
    reader = zpf.BlockReader(io.BytesIO(write_file(*file)))
    assert list(reader) == expected
    assert reader.complete == with_end
    assert not reader.truncated
    assert reader.diagnostics == []


@given(files)
def test_read_reemit_is_byte_identical(file: ZpfFile):
    data = write_file(*file)
    blocks = list(zpf.BlockReader(io.BytesIO(data)))
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        for block in blocks:
            writer.write(block)
    assert sink.getvalue() == data


@given(files)
def test_canonical_encoding_is_stable_after_replace(file: ZpfFile):
    data = write_file(*file)
    blocks = [dataclasses.replace(block) for block in zpf.BlockReader(io.BytesIO(data))]
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        for block in blocks:
            writer.write(block)
    assert sink.getvalue() == data


def jsonl_normalized(block: zpf.Block) -> zpf.Block:
    """Apply the documented JSONL normalizations to an expected block.

    ``TcpRole.UNKNOWN`` projects as an omitted key (spec-mandated). Record
    flag bits with no JSON token would be dropped too, but the generator
    does not produce those — see ``_DEFINED_RECORD_FLAGS``.
    """
    if isinstance(block, zpf.Participant) and block.tcp_role == zpf.TcpRole.UNKNOWN:
        return dataclasses.replace(block, tcp_role=None)
    return block


@given(files)
def test_jsonl_round_trip_is_semantically_lossless(file: ZpfFile):
    header, body, with_end = file
    expected = [jsonl_normalized(b) for b in [header, *body] + ([zpf.End()] if with_end else [])]
    text = io.StringIO()
    with zpf.JsonlWriter(text) as writer:
        writer.write(header)
        for block in body:
            writer.write(block)
        if with_end:
            writer.write(zpf.End())
    text.seek(0)
    reader = zpf.JsonlReader(text)
    assert list(reader) == expected
    assert reader.complete == with_end
    assert not reader.truncated


@given(files)
def test_binary_jsonl_binary_jsonl_is_stable(file: ZpfFile):
    binary_1 = io.BytesIO(write_file(*file))
    text_1 = io.StringIO()
    zpf.binary_to_jsonl(binary_1, text_1)
    text_1.seek(0)
    binary_2 = io.BytesIO()
    zpf.jsonl_to_binary(text_1, binary_2)
    binary_2.seek(0)
    text_2 = io.StringIO()
    zpf.binary_to_jsonl(binary_2, text_2)
    assert text_2.getvalue() == text_1.getvalue()


@given(st.binary(max_size=300))
def test_arbitrary_bytes_never_crash_the_reader(data: bytes):
    reader = zpf.BlockReader(io.BytesIO(data))
    # StructuralError is the only permitted exception.
    with contextlib.suppress(zpf.StructuralError):
        list(reader)


@given(file_headers, st.binary(max_size=200))
def test_bytes_after_a_valid_header_never_crash_the_reader(header: zpf.FileHeader, junk: bytes):
    data = write_file(header, [], False) + junk
    reader = zpf.BlockReader(io.BytesIO(data))
    with contextlib.suppress(zpf.StructuralError):
        list(reader)

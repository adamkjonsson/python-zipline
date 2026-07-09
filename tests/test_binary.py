"""Unit tests for BlockReader/BlockWriter: framing, structural tier, truncation."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest

import zpf
from zpf import _frame

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HEADER = zpf.FileHeader(tick_hz=1_000_000)
SESSION = zpf.Session(session_id=1, proto="tcp")
RECORD = zpf.Record(session_id=1, sender_pid=0, source_id=0, timestamp=10, payload=b"hi")


def build(*blocks: zpf.Block) -> bytes:
    """Serialize blocks (with framing) without BlockWriter's checks."""
    out = bytearray()
    for block in blocks:
        content = block.to_bytes()
        out += _frame.FRAME.pack(block.block_type, 0, len(content))
        out += content
    return bytes(out)


def write_file(*blocks: zpf.Block) -> bytes:
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        for block in blocks:
            writer.write(block)
    return sink.getvalue()


def test_path_round_trip(tmp_path: Path):
    path = tmp_path / "out.zpf"
    with zpf.BlockWriter(path) as writer:
        for block in (HEADER, SESSION, RECORD, zpf.End()):
            writer.write(block)
    with zpf.BlockReader(path) as reader:
        assert list(reader) == [HEADER, SESSION, RECORD, zpf.End()]
        assert reader.complete
        assert not reader.truncated


def test_stream_round_trip_and_write_offsets():
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as writer:
        assert writer.write(HEADER) == 0
        second = writer.write(SESSION)
        assert second == 8 + len(HEADER.to_bytes())
        assert writer.offset == second + 8 + len(SESSION.to_bytes())
    reader = zpf.BlockReader(io.BytesIO(sink.getvalue()))
    assert list(reader) == [HEADER, SESSION]
    assert reader.header == HEADER
    assert not reader.complete  # no End block


def test_unknown_block_types_round_trip():
    unknown = zpf.UnknownBlock(block_type=0x0F42, content=b"\x01\x02\x03\x04")
    data = write_file(HEADER, unknown, SESSION)
    assert list(zpf.BlockReader(io.BytesIO(data))) == [HEADER, unknown, SESSION]


def test_writer_requires_header_first():
    with zpf.BlockWriter(io.BytesIO()) as writer, pytest.raises(zpf.StructuralError):
        writer.write(SESSION)


def test_writer_rejects_second_header():
    with zpf.BlockWriter(io.BytesIO()) as writer:
        writer.write(HEADER)
        with pytest.raises(zpf.StructuralError):
            writer.write(HEADER)


def test_writer_rejects_blocks_after_end():
    with zpf.BlockWriter(io.BytesIO()) as writer:
        writer.write(HEADER)
        writer.write(zpf.End())
        with pytest.raises(zpf.StructuralError):
            writer.write(SESSION)


def test_writer_rejects_writes_after_close():
    writer = zpf.BlockWriter(io.BytesIO())
    writer.write(HEADER)
    writer.close()
    with pytest.raises(zpf.ZpfError):
        writer.write(SESSION)


def test_empty_file_is_structural():
    with pytest.raises(zpf.StructuralError):
        next(iter(zpf.BlockReader(io.BytesIO(b""))))


def test_first_block_not_a_header_is_structural():
    with pytest.raises(zpf.StructuralError):
        list(zpf.BlockReader(io.BytesIO(build(SESSION))))


def test_second_header_is_structural():
    data = build(HEADER, HEADER)
    with pytest.raises(zpf.StructuralError):
        list(zpf.BlockReader(io.BytesIO(data)))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda c: c[:8] + b"\xff\xff\xff\xff" + c[12:], "bad magic"),
        (lambda c: c[:8] + bytes.fromhex("5a495046") + c[12:], "byte-swapped"),
        (lambda c: c[:12] + (2).to_bytes(2, "little") + c[14:], "version_major"),
        (lambda c: c[:16] + bytes(8), "tick_hz"),
    ],
)
def test_bad_header_fields_are_structural(mutate: Callable[[bytes], bytes], match: str):
    with pytest.raises(zpf.StructuralError, match=match):
        list(zpf.BlockReader(io.BytesIO(mutate(build(HEADER)))))


def test_unaligned_block_length_is_structural():
    data = build(HEADER) + _frame.FRAME.pack(0x10, 0, 10) + bytes(12)
    with pytest.raises(zpf.StructuralError, match="multiple of 4"):
        list(zpf.BlockReader(io.BytesIO(data)))


def test_tlv_overrun_is_structural():
    content = SESSION.to_bytes() + _frame.OPTION_HEADER.pack(0x0FFF, 500)
    data = build(HEADER) + _frame.FRAME.pack(zpf.Session.block_type, 0, len(content)) + content
    with pytest.raises(zpf.StructuralError, match="overruns"):
        list(zpf.BlockReader(io.BytesIO(data)))


@pytest.mark.parametrize("cut", [3, 11])  # mid-frame and mid-content of the second block
def test_truncation_keeps_prior_blocks(cut: int):
    data = build(HEADER)
    reader = zpf.BlockReader(io.BytesIO(data + build(SESSION)[:cut]))
    assert list(reader) == [HEADER]
    assert reader.truncated
    assert not reader.complete
    assert reader.diagnostics[0].category == "truncated"
    assert reader.diagnostics[0].offset == len(data)


def test_truncation_raises_in_strict_mode():
    data = build(HEADER, SESSION)[:-2]
    reader = zpf.BlockReader(io.BytesIO(data), strict=True)
    with pytest.raises(zpf.TruncatedError):
        list(reader)
    assert reader.truncated


def test_trailing_bytes_after_end_are_reported():
    data = write_file(HEADER, zpf.End()) + b"junk"
    reader = zpf.BlockReader(io.BytesIO(data))
    assert list(reader) == [HEADER, zpf.End()]
    assert reader.complete  # the file up to End stays valid and complete
    assert reader.diagnostics[0].category == "trailing-bytes"


def test_trailing_bytes_raise_in_strict_mode():
    data = write_file(HEADER, zpf.End()) + b"junk"
    with pytest.raises(zpf.SemanticError):
        list(zpf.BlockReader(io.BytesIO(data), strict=True))


def test_semantically_invalid_block_is_isolated_by_default():
    bad_end = zpf.UnknownBlock(block_type=zpf.End.block_type, content=b"\x00\x00\x00\x00")
    data = build(HEADER, bad_end, SESSION)
    reader = zpf.BlockReader(io.BytesIO(data))
    blocks = list(reader)
    assert blocks == [HEADER, bad_end, SESSION]  # preserved, not reinterpreted as End
    assert not reader.complete
    assert reader.diagnostics[0].category == "invalid-block"


def test_semantically_invalid_block_raises_in_strict_mode():
    bad_end = zpf.UnknownBlock(block_type=zpf.End.block_type, content=b"\x00\x00\x00\x00")
    data = build(HEADER, bad_end)
    with pytest.raises(zpf.SemanticError):
        list(zpf.BlockReader(io.BytesIO(data), strict=True))


def test_reader_over_a_non_seekable_stream():
    class OneByteStream(io.RawIOBase):
        """A stream that returns at most one byte per read and cannot seek."""

        def __init__(self, data: bytes):
            self._data = io.BytesIO(data)

        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            return self._data.read(min(size, 1) if size and size > 0 else size)

    reader = zpf.BlockReader(OneByteStream(write_file(HEADER, SESSION, zpf.End())))
    assert list(reader) == [HEADER, SESSION, zpf.End()]
    assert reader.complete

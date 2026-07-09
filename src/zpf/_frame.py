"""Low-level byte machinery for the ZPF binary container.

Everything in the container is little-endian. A file is a sequence of blocks,
each with an 8-byte frame (``type: u16, reserved: u16, length: u32``); the
``length`` counts the bytes after the length field — body, TLV options, and
padding — and is always a multiple of 4, so every block and every block's
content start on a 4-byte boundary. TLV options are ``id: u16, len: u16,
value`` with the value zero-padded to 4 bytes (the padding never counted in
``len``).

This module holds the framing/TLV codecs and the numeric constants of the
specification (magic values, block type codes, the option-id registry). The
typed view of blocks lives in :mod:`zpf.blocks`.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, NamedTuple

from zpf.errors import EncodeError, StructuralError

if TYPE_CHECKING:
    from collections.abc import Iterator

# File signatures (values are u32; on disk always their little-endian bytes).
MAGIC = 0x5A495046  # "ZIPF"
END_MAGIC = 0x5A454E44  # "ZEND"

# Block type codes.
BT_FILE_HEADER = 0x01
BT_SOURCE = 0x02
BT_DECODER = 0x03
BT_SESSION = 0x10
BT_PARTICIPANT = 0x11
BT_SESSION_END = 0x12
BT_RECORD = 0x20
BT_UNDECODED = 0x21
BT_NAME_RESOLUTION = 0x30
BT_END = 0x41
BT_CUSTOM = 0xFF

# TLV option-id registry.
OPT_COMMENT = 0x0001
OPT_TIME_EPOCH = 0x0010
OPT_CREATOR = 0x0011
OPT_PRODUCED_BY = 0x0012
OPT_PRODUCED_AT = 0x0013
OPT_FILE_FLAGS = 0x0014
OPT_URI = 0x0020
OPT_DIGEST = 0x0021
OPT_LINK_TYPE = 0x0022
OPT_NAME = 0x0041
OPT_VERSION = 0x0042
OPT_PARAMS_DIGEST = 0x0043
OPT_PROTO = 0x0050
OPT_FLOW_KEY = 0x0051
OPT_SESSION_FLAGS = 0x0052
OPT_ENDPOINT = 0x0060
OPT_ISN = 0x0061
OPT_IDENTITY = 0x0062
OPT_TCP_ROLE = 0x0063
OPT_ORIGIN = 0x0064
OPT_SEQ_START = 0x0070
OPT_ACK = 0x0072
OPT_TS_FIRST = 0x0073
OPT_SPANS = 0x0080
OPT_DECODER_ID = 0x0090
OPT_CONTENT_TYPE = 0x0091
OPT_UNDECODED_REASON = 0x00A0
OPT_LABEL = 0x00B0
OPT_LABEL_KIND = 0x00B1
OPT_SESSION_END_REASON = 0x00C0

# Struct codecs.
FRAME = struct.Struct("<HHI")  # type, reserved, length
OPTION_HEADER = struct.Struct("<HH")  # id, len
SPAN_ENTRY = struct.Struct("<HHQQQ")  # source_id, pid, session_id, off_start, off_end
ORIGIN = struct.Struct("<HHQ")  # source_id, pid, session_id

U8 = struct.Struct("<B")
U16 = struct.Struct("<H")
U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")
I64 = struct.Struct("<q")

SPAN_ENTRY_SIZE = SPAN_ENTRY.size  # 28
MAX_OPTION_VALUE = 0xFFFF
MAX_SPANS_PER_OPTION = MAX_OPTION_VALUE // SPAN_ENTRY_SIZE  # 2340

FRAME_SIZE = FRAME.size  # 8
MAX_BLOCK_LENGTH = 0xFFFF_FFFF


class RawOption(NamedTuple):
    """One TLV option occurrence, preserved as raw bytes.

    Used for option ids a parser does not recognize — and for occurrences it
    cannot interpret (a malformed value, or a duplicate of a single-valued
    id) — so that every occurrence of every option survives a round-trip, as
    the specification requires.

    Attributes:
        option_id: The TLV ``id`` (u16).
        value: The unpadded option value bytes.

    """

    option_id: int
    value: bytes


def pad4(length: int) -> int:
    """Return the number of zero bytes needed to pad ``length`` to 4."""
    return -length % 4


def padded(data: bytes) -> bytes:
    """Return ``data`` zero-padded to a multiple of 4 bytes."""
    return data + b"\x00" * pad4(len(data))


def encode_option(option_id: int, value: bytes) -> bytes:
    """Encode one TLV option, including its trailing padding.

    Args:
        option_id: The TLV ``id``.
        value: The unpadded value bytes.

    Returns:
        The encoded option: header, value, and zero padding to 4 bytes.

    Raises:
        EncodeError: If ``value`` exceeds the 65 535-byte TLV value cap.

    """
    if len(value) > MAX_OPTION_VALUE:
        msg = f"option 0x{option_id:04X} value is {len(value)} bytes; TLV cap is {MAX_OPTION_VALUE}"
        raise EncodeError(msg)
    return OPTION_HEADER.pack(option_id, len(value)) + padded(value)


def iter_options(data: bytes) -> Iterator[RawOption]:
    """Iterate the TLV options in a block's option region.

    Args:
        data: The option region — everything after the block's fixed body,
            running to the end of the block content.

    Yields:
        Each option occurrence in file order, with unpadded value bytes.

    Raises:
        StructuralError: If an option header or value overruns the region.

    """
    pos = 0
    end = len(data)
    while pos < end:
        if end - pos < OPTION_HEADER.size:
            msg = "TLV option header overruns its block"
            raise StructuralError(msg)
        option_id, value_len = OPTION_HEADER.unpack_from(data, pos)
        pos += OPTION_HEADER.size
        if pos + value_len > end:
            msg = f"TLV option 0x{option_id:04X} value (len {value_len}) overruns its block"
            raise StructuralError(msg)
        yield RawOption(option_id, data[pos : pos + value_len])
        pos += value_len + pad4(value_len)

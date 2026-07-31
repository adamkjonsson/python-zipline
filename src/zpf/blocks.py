"""Typed block model for the Zipline Payload Format v0.9.

One frozen dataclass per block type, mirroring the specification's normative
binary encoding. Each class knows how to serialize itself (:meth:`Block.to_bytes`)
and parse itself (:meth:`Block.from_content`); :func:`parse_block` dispatches on a
block type code.

Round-trip guarantees:

* **Byte-exact for unmodified blocks** — a parsed block caches its original
  content and re-serializes to identical bytes. Editing a block with
  :func:`dataclasses.replace` drops the cache, so the copy re-serializes
  canonically (typed options in ascending registry-id order, then
  ``extra_options``).
* **Nothing is dropped** — option ids a parser does not recognize, occurrences
  it cannot interpret (malformed values), and duplicates of single-valued ids
  are preserved in file order in ``extra_options``, as the specification
  requires.
"""

from __future__ import annotations

import contextlib
import struct
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from itertools import chain
from typing import Any, Callable, ClassVar, NamedTuple, Self

from zpf import _frame
from zpf._frame import RawOption
from zpf.content import ContentType, decode_prim, prim_fault
from zpf.errors import ContentError, EncodeError, SemanticError, StructuralError

# A byte-swapped (big-endian, invalid) file's magic reads as this value.
_SWAPPED_MAGIC = 0x4650495A

#: The specification version this implementation reads and writes, as
#: ``(version_major, version_minor)``. A writer stamps the version it
#: implements; there is no obligation to compute the lowest version whose
#: features a file happens to use.
SPEC_VERSION: tuple[int, int] = (0, 12)


def unsupported_version(version_major: int, version_minor: int) -> str | None:
    """Report why a file's stamped version cannot be read, if it cannot.

    The specification has two compatibility regimes. While ``version_major``
    is ``0`` the pair is the compatibility identity, so a reader MUST reject a
    ``version_minor`` it does not implement exactly as it rejects an unknown
    major. From ``1.0`` onward a minor bump only adds skippable surface, and a
    reader MUST NOT gate on the minor at all.

    Args:
        version_major: The file's stamped major version.
        version_minor: The file's stamped minor version.

    Returns:
        A diagnostic naming the offending field, or ``None`` if supported.

    """
    major, minor = SPEC_VERSION
    if version_major != major:
        return f"unsupported version_major {version_major}; this implementation reads {major}"
    if major == 0 and version_minor != minor:
        return (
            f"unsupported version_minor {version_minor}; while version_major is 0 every "
            f"minor is a separate format and this implementation reads {minor}"
        )
    return None


class SourceKind(IntEnum):
    """The ``kind`` of a Source Descriptor."""

    CAPTURE = 0
    """A raw capture source (a pcap file or interface)."""

    ZPF_INPUT = 1
    """Another ``.zpf`` file this file was derived from."""


class TcpRole(IntEnum):
    """Which side of an observed TCP handshake a participant was."""

    UNKNOWN = 0
    INITIATOR = 1
    RESPONDER = 2


class FileFlags(IntFlag):
    """File Header ``flags`` bitfield (u16)."""

    SINGLE_CLOCK = 0x0001
    """Every record in the file was stamped against one trustworthy clock."""


class SessionFlags(IntFlag):
    """Session Descriptor ``flags`` bitfield (u16)."""

    SEQUENCED = 0x0001
    """Record-block file order is a valid causal linearization."""


class RecordFlags(IntFlag):
    """Record ``flags`` bitfield (u16)."""

    PSH = 0x0001
    FIN = 0x0002
    RST = 0x0004
    SYN = 0x0008
    URG = 0x0010
    RETRANSMIT = 0x0040
    MESSAGE = 0x0080


def _check_uint(value: int, bits: int, name: str) -> None:
    """Raise :class:`EncodeError` unless ``value`` fits an unsigned ``bits``-wide field."""
    if not 0 <= int(value) < 1 << bits:
        msg = f"{name} out of range for u{bits}: {value}"
        raise EncodeError(msg)


def _check_uint_opt(value: int | None, bits: int, name: str) -> None:
    """Range-check an optional unsigned field."""
    if value is not None:
        _check_uint(value, bits, name)


def _check_i64(value: int, name: str) -> None:
    """Raise :class:`EncodeError` unless ``value`` fits a signed 64-bit field."""
    if not -(1 << 63) <= value < 1 << 63:
        msg = f"{name} out of range for i64: {value}"
        raise EncodeError(msg)


def _check_i64_opt(value: int | None, name: str) -> None:
    """Range-check an optional signed 64-bit field."""
    if value is not None:
        _check_i64(value, name)


@dataclass(frozen=True)
class Span:
    """One source range a record's bytes were built from (half-open offsets).

    ``session_id`` and ``participant_id`` are read in the referenced
    *source's* id namespace, never the citing file's. For a ``zpf-input``
    source the offsets are logical 0-based stream offsets; for a ``capture``
    source they are byte offsets into the capture file and
    ``session_id``/``participant_id`` are unused (written 0).

    Attributes:
        source_id: The Source Descriptor being cited.
        session_id: Session inside that source.
        participant_id: Participant (stream) inside that source.
        off_start: First byte of the range.
        off_end: One past the last byte of the range.

    """

    source_id: int
    session_id: int
    participant_id: int
    off_start: int
    off_end: int

    def __post_init__(self) -> None:
        _check_uint(self.source_id, 16, "source_id")
        _check_uint(self.session_id, 64, "session_id")
        _check_uint(self.participant_id, 16, "participant_id")
        _check_uint(self.off_start, 64, "off_start")
        _check_uint(self.off_end, 64, "off_end")
        if self.off_end < self.off_start:
            msg = f"span off_end {self.off_end} precedes off_start {self.off_start}"
            raise EncodeError(msg)

    def pack(self) -> bytes:
        """Return the 28-byte packed form (u16s lead for alignment)."""
        return _frame.SPAN_ENTRY.pack(
            self.source_id, self.participant_id, self.session_id, self.off_start, self.off_end
        )


@dataclass(frozen=True)
class Origin:
    """The input stream a pass-through participant re-emits.

    Ids are in the referenced source's namespace, exactly as a
    :class:`Span`'s are.

    Attributes:
        source_id: A ``zpf-input`` Source declared in the citing file.
        session_id: Session inside that source.
        participant_id: Participant inside that source.

    """

    source_id: int
    session_id: int
    participant_id: int

    def __post_init__(self) -> None:
        _check_uint(self.source_id, 16, "source_id")
        _check_uint(self.session_id, 64, "session_id")
        _check_uint(self.participant_id, 16, "participant_id")

    def pack(self) -> bytes:
        """Return the 12-byte packed form (u16s lead for alignment)."""
        return _frame.ORIGIN.pack(self.source_id, self.participant_id, self.session_id)


# --- Option value codecs -----------------------------------------------------
# Decoders raise ValueError / struct.error / UnicodeDecodeError / EncodeError
# on values they cannot interpret; the parser then preserves the occurrence
# in ``extra_options`` instead of failing.


def _unpack_str(value: bytes) -> str:
    return value.decode("utf-8")


def _pack_str(value: str) -> bytes:
    return value.encode("utf-8")


def _unpack_u8(value: bytes) -> int:
    return _frame.U8.unpack(value)[0]


def _unpack_u16(value: bytes) -> int:
    return _frame.U16.unpack(value)[0]


def _unpack_u32(value: bytes) -> int:
    return _frame.U32.unpack(value)[0]


def _unpack_i64(value: bytes) -> int:
    return _frame.I64.unpack(value)[0]


def _pack_u8(value: int) -> bytes:
    return _frame.U8.pack(int(value))


def _pack_u16(value: int) -> bytes:
    return _frame.U16.pack(int(value))


def _pack_u32(value: int) -> bytes:
    return _frame.U32.pack(int(value))


def _pack_i64(value: int) -> bytes:
    return _frame.I64.pack(int(value))


def _unpack_tcp_role(value: bytes) -> TcpRole | int:
    """Read a ``tcp_role``, keeping a value the enum does not define.

    The option is advisory, so an unrecognised value means "unknown" — a
    reader carries it and moves on rather than treating it as an error.

    Args:
        value: The option's raw bytes.

    Returns:
        The enum member, or the raw number when none is defined.

    """
    raw = _unpack_u8(value)
    try:
        return TcpRole(raw)
    except ValueError:
        return raw


def _unpack_file_flags(value: bytes) -> FileFlags:
    return FileFlags(_unpack_u16(value))


def _unpack_session_flags(value: bytes) -> SessionFlags:
    return SessionFlags(_unpack_u16(value))


def _unpack_origin(value: bytes) -> Origin:
    source_id, participant_id, session_id = _frame.ORIGIN.unpack(value)
    return Origin(source_id=source_id, session_id=session_id, participant_id=participant_id)


def _pack_origin(value: Origin) -> bytes:
    return value.pack()


def _unpack_spans_chunk(value: bytes) -> tuple[Span, ...]:
    if len(value) % _frame.SPAN_ENTRY_SIZE:
        msg = f"spans option length {len(value)} is not a multiple of {_frame.SPAN_ENTRY_SIZE}"
        raise ValueError(msg)
    return tuple(
        Span(
            source_id=source_id,
            session_id=session_id,
            participant_id=participant_id,
            off_start=off_start,
            off_end=off_end,
        )
        for source_id, participant_id, session_id, off_start, off_end in (
            _frame.SPAN_ENTRY.iter_unpack(value)
        )
    )


def _pack_never(value: object) -> bytes:
    """Raise: options serialized by mode-specific code never call their encoder."""
    msg = f"unreachable: {value!r} is encoded by its option's mode"
    raise EncodeError(msg)


def _spans_chunks(spans: tuple[Span, ...]) -> list[bytes]:
    """Pack a span list into one or more option values (2340 entries each)."""
    step = _frame.MAX_SPANS_PER_OPTION
    return [
        b"".join(span.pack() for span in spans[i : i + step]) for i in range(0, len(spans), step)
    ]


class _OptSpec(NamedTuple):
    """How one registered TLV option id maps onto a dataclass field."""

    option_id: int
    field: str
    decode: Callable[[bytes], Any]
    encode: Callable[[Any], bytes]
    mode: str = "single"  # "single" | "list" (element per option) | "concat" (chunked list)
    skip_zero: bool = False  # omit when the value is falsy (zero flags bitfield)


class _SpecTable(NamedTuple):
    """A block's option-spec table: canonical encode order plus id index."""

    ordered: tuple[_OptSpec, ...]
    by_id: dict[int, _OptSpec]


def _specs(*specs: _OptSpec) -> _SpecTable:
    """Return the spec table sorted by option id, plus an id-keyed index."""
    ordered = tuple(sorted(specs, key=lambda s: s.option_id))
    return _SpecTable(ordered, {s.option_id: s for s in ordered})


_COMMENT_SPEC = _OptSpec(_frame.OPT_COMMENT, "comment", _unpack_str, _pack_str)


def _parse_options(
    data: bytes, specs_by_id: dict[int, _OptSpec]
) -> tuple[dict[str, Any], tuple[RawOption, ...]]:
    """Parse a block's option region against its spec table.

    Returns:
        A dict of field values for recognized options, and the preserved
        raw occurrences (unknown ids, malformed values, duplicates of
        single-valued ids) in file order.

    """
    values: dict[str, Any] = {}
    extras: list[RawOption] = []
    for opt in _frame.iter_options(data):
        spec = specs_by_id.get(opt.option_id)
        if spec is None:
            extras.append(opt)
            continue
        try:
            value = spec.decode(opt.value)
        except (ValueError, struct.error, UnicodeDecodeError, EncodeError):
            extras.append(opt)
            continue
        if spec.mode == "single":
            if spec.field in values:
                extras.append(opt)
            else:
                values[spec.field] = value
        else:
            values.setdefault(spec.field, []).append(value)
    for spec in specs_by_id.values():
        if spec.field in values and spec.mode == "list":
            values[spec.field] = tuple(values[spec.field])
        elif spec.field in values and spec.mode == "concat":
            values[spec.field] = tuple(chain.from_iterable(values[spec.field]))
    return values, tuple(extras)


def _encode_options(block: Block, specs: tuple[_OptSpec, ...]) -> bytes:
    """Canonically encode a block's typed options followed by its extras."""
    parts: list[bytes] = []
    for spec in specs:
        value = getattr(block, spec.field)
        if value is None or (spec.mode != "single" and not value) or (spec.skip_zero and not value):
            continue
        if spec.mode == "single":
            raws = [spec.encode(value)]
        elif spec.mode == "list":
            raws = [spec.encode(element) for element in value]
        else:
            raws = _spans_chunks(value)
        parts.extend(_frame.encode_option(spec.option_id, raw) for raw in raws)
    extras: tuple[RawOption, ...] = getattr(block, "extra_options", ())
    parts.extend(_frame.encode_option(opt.option_id, opt.value) for opt in extras)
    return b"".join(parts)


# --- Blocks ------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """Base class for all ZPF blocks.

    Subclasses are frozen dataclasses; edit them with
    :func:`dataclasses.replace`, which also drops the cached original bytes
    so the copy re-serializes canonically.
    """

    block_type: ClassVar[int]

    _content: bytes | None = field(default=None, init=False, compare=False, repr=False)

    def to_bytes(self) -> bytes:
        """Serialize the block's frame content (excluding the 8-byte frame).

        A block parsed from bytes and not modified since returns its original
        content byte-for-byte; otherwise the canonical encoding is produced.

        Returns:
            The block content — fixed body, options, and padding, always a
            multiple of 4 bytes.

        """
        if self._content is not None:
            return self._content
        return self._encode()

    @classmethod
    def from_content(cls, content: bytes) -> Self:
        """Parse a block of this type from its frame content.

        Args:
            content: The block's content bytes (everything after the 8-byte
                frame).

        Returns:
            The parsed block, carrying ``content`` cached for byte-exact
            re-serialization.

        Raises:
            StructuralError: If the body or an option overruns the content,
                or (File Header) the magic/version/``tick_hz`` is invalid.
            SemanticError: If the content is well-framed but invalid (e.g.
                a wrong End-block magic).

        """
        block = cls._parse(content)
        object.__setattr__(block, "_content", content)
        return block

    def _encode(self) -> bytes:
        raise NotImplementedError

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        raise NotImplementedError

    @classmethod
    def _body(cls, content: bytes, body: struct.Struct) -> tuple[Any, ...]:
        """Unpack the fixed body, raising :class:`StructuralError` if short."""
        if len(content) < body.size:
            msg = f"{cls.__name__} body ({body.size} bytes) overruns its block ({len(content)})"
            raise StructuralError(msg)
        return body.unpack_from(content)


_FILE_HEADER_BODY = struct.Struct("<IHHQ")


@dataclass(frozen=True)
class FileHeader(Block):
    """File Header block (``0x01``) — must be the first block in a file.

    Attributes:
        tick_hz: Time units per second for all timestamps; must be non-zero.
        version_major: Format major version; see :data:`SPEC_VERSION`.
        version_minor: Format minor version. While the major is ``0`` this
            selects the format outright — ``0.12`` and ``0.11`` are different
            formats, not compatible refinements.
        time_epoch: Origin for record timestamps, in ``tick_hz`` ticks since
            the Unix epoch; ``None`` means the default origin (0).
        creator: Tool + version that wrote the file.
        produced_by: Tool + version that ran the transform (derived files).
        produced_at: Wall-clock build time in Unix seconds (derived files).
        flags: File-level flags (:class:`FileFlags`).
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_FILE_HEADER

    tick_hz: int
    version_major: int = SPEC_VERSION[0]
    version_minor: int = SPEC_VERSION[1]
    time_epoch: int | None = None
    creator: str | None = None
    produced_by: str | None = None
    produced_at: int | None = None
    flags: FileFlags = FileFlags(0)
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_TIME_EPOCH, "time_epoch", _unpack_i64, _pack_i64),
        _OptSpec(_frame.OPT_CREATOR, "creator", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_PRODUCED_BY, "produced_by", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_PRODUCED_AT, "produced_at", _unpack_i64, _pack_i64),
        _OptSpec(_frame.OPT_FILE_FLAGS, "flags", _unpack_file_flags, _pack_u16, skip_zero=True),
    )

    def __post_init__(self) -> None:
        _check_uint(self.tick_hz, 64, "tick_hz")
        if self.tick_hz == 0:
            msg = "tick_hz must be non-zero"
            raise EncodeError(msg)
        _check_uint(self.version_minor, 16, "version_minor")
        unsupported = unsupported_version(self.version_major, self.version_minor)
        if unsupported is not None:
            raise EncodeError(unsupported)
        _check_i64_opt(self.time_epoch, "time_epoch")
        _check_i64_opt(self.produced_at, "produced_at")
        _check_uint(int(self.flags), 16, "flags")
        object.__setattr__(self, "flags", FileFlags(self.flags))
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    @property
    def single_clock(self) -> bool:
        """Whether the SINGLE_CLOCK file flag is set."""
        return bool(self.flags & FileFlags.SINGLE_CLOCK)

    def _encode(self) -> bytes:
        body = _FILE_HEADER_BODY.pack(
            _frame.MAGIC, self.version_major, self.version_minor, self.tick_hz
        )
        return body + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        magic, version_major, version_minor, tick_hz = cls._body(content, _FILE_HEADER_BODY)
        if magic == _SWAPPED_MAGIC:
            msg = "byte-swapped magic: this file was written big-endian and is invalid"
            raise StructuralError(msg)
        if magic != _frame.MAGIC:
            msg = f"bad magic 0x{magic:08X}; not a ZPF file"
            raise StructuralError(msg)
        unsupported = unsupported_version(version_major, version_minor)
        if unsupported is not None:
            raise StructuralError(unsupported)
        if tick_hz == 0:
            msg = "tick_hz must be non-zero"
            raise StructuralError(msg)
        values, extras = _parse_options(content[_FILE_HEADER_BODY.size :], cls._SPEC.by_id)
        return cls(
            tick_hz=tick_hz,
            version_major=version_major,
            version_minor=version_minor,
            extra_options=extras,
            **values,
        )


_SOURCE_BODY = struct.Struct("<HBB")


@dataclass(frozen=True)
class Source(Block):
    """Source Descriptor block (``0x02``) — one input these bytes came from.

    Attributes:
        source_id: Id referenced by records and spans.
        kind: :attr:`SourceKind.CAPTURE` or :attr:`SourceKind.ZPF_INPUT`
            (an unrecognized u8 value is kept as a plain ``int``).
        uri: Where the referenced capture/input file lives.
        digest: Content hash of the referenced file (``"<alg>:<hex>"``).
        link_type: Link-layer type (capture sources only).
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_SOURCE

    source_id: int
    kind: SourceKind | int
    uri: str | None = None
    digest: str | None = None
    link_type: int | None = None
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_URI, "uri", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_DIGEST, "digest", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_LINK_TYPE, "link_type", _unpack_u16, _pack_u16),
    )

    def __post_init__(self) -> None:
        _check_uint(self.source_id, 16, "source_id")
        _check_uint(int(self.kind), 8, "kind")
        _check_uint_opt(self.link_type, 16, "link_type")
        if not isinstance(self.kind, SourceKind) and int(self.kind) in (0, 1):
            object.__setattr__(self, "kind", SourceKind(self.kind))
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    def _encode(self) -> bytes:
        body = _SOURCE_BODY.pack(self.source_id, int(self.kind), 0)
        return body + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        source_id, kind, _reserved = cls._body(content, _SOURCE_BODY)
        values, extras = _parse_options(content[_SOURCE_BODY.size :], cls._SPEC.by_id)
        return cls(source_id=source_id, kind=kind, extra_options=extras, **values)


_DECODER_BODY = struct.Struct("<HH")


@dataclass(frozen=True)
class Decoder(Block):
    """Decoder Descriptor block (``0x03``) — decode-stage files only.

    Attributes:
        decoder_id: Id referenced per-record.
        name: Decoder identifier, e.g. ``"http/1.1"``.
        version: Decoder version.
        params_digest: Hash of the decoder config, for reproducible decodes.
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_DECODER

    decoder_id: int
    name: str | None = None
    version: str | None = None
    params_digest: str | None = None
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_NAME, "name", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_VERSION, "version", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_PARAMS_DIGEST, "params_digest", _unpack_str, _pack_str),
    )

    def __post_init__(self) -> None:
        _check_uint(self.decoder_id, 16, "decoder_id")
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    def _encode(self) -> bytes:
        return _DECODER_BODY.pack(self.decoder_id, 0) + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        decoder_id, _reserved = cls._body(content, _DECODER_BODY)
        values, extras = _parse_options(content[_DECODER_BODY.size :], cls._SPEC.by_id)
        return cls(decoder_id=decoder_id, extra_options=extras, **values)


_SESSION_BODY = struct.Struct("<Q")


@dataclass(frozen=True)
class Session(Block):
    """Session Descriptor block (``0x10``).

    Attributes:
        session_id: Id referenced by participants and records.
        proto: Session protocol (lowercase; e.g. ``"tcp"``, ``"http"``).
        flow_key: Human-readable flow key, e.g. ``"a:port <-> b:port"``.
        flags: Session-level flags (:class:`SessionFlags`).
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_SESSION

    session_id: int
    proto: str | None = None
    flow_key: str | None = None
    flags: SessionFlags = SessionFlags(0)
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_PROTO, "proto", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_FLOW_KEY, "flow_key", _unpack_str, _pack_str),
        _OptSpec(
            _frame.OPT_SESSION_FLAGS, "flags", _unpack_session_flags, _pack_u16, skip_zero=True
        ),
    )

    def __post_init__(self) -> None:
        _check_uint(self.session_id, 64, "session_id")
        _check_uint(int(self.flags), 16, "flags")
        object.__setattr__(self, "flags", SessionFlags(self.flags))
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    @property
    def sequenced(self) -> bool:
        """Whether the SEQUENCED session flag is set."""
        return bool(self.flags & SessionFlags.SEQUENCED)

    def _encode(self) -> bytes:
        return _SESSION_BODY.pack(self.session_id) + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        (session_id,) = cls._body(content, _SESSION_BODY)
        values, extras = _parse_options(content[_SESSION_BODY.size :], cls._SPEC.by_id)
        return cls(session_id=session_id, extra_options=extras, **values)


_PARTICIPANT_BODY = struct.Struct("<QHH")


@dataclass(frozen=True)
class Participant(Block):
    """Participant Descriptor block (``0x11``).

    Attributes:
        session_id: Session this participant belongs to.
        participant_id: Id within that session (the ``pid``).
        endpoints: Participant addresses, ordered outermost tunnel layer
            first; the last entry is the address at which the participant
            speaks the session protocol. Repeatable option.
        isn: The SYN's TCP sequence number; must be present when the
            handshake was observed (fixes the stream's absolute origin).
        identity: Stable identity distinct from a transient endpoint.
        tcp_role: Which side opened the connection, when known. Advisory, so
            a value the enum does not define is carried as a plain ``int``
            and means "unknown" rather than being an error.
        origin: Input stream mapping (pass-through files only).
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_PARTICIPANT

    session_id: int
    participant_id: int
    endpoints: tuple[str, ...] = ()
    isn: int | None = None
    identity: str | None = None
    tcp_role: TcpRole | int | None = None
    origin: Origin | None = None
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_ENDPOINT, "endpoints", _unpack_str, _pack_str, mode="list"),
        _OptSpec(_frame.OPT_ISN, "isn", _unpack_u32, _pack_u32),
        _OptSpec(_frame.OPT_IDENTITY, "identity", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_TCP_ROLE, "tcp_role", _unpack_tcp_role, _pack_u8),
        _OptSpec(_frame.OPT_ORIGIN, "origin", _unpack_origin, _pack_origin),
    )

    def __post_init__(self) -> None:
        _check_uint(self.session_id, 64, "session_id")
        _check_uint(self.participant_id, 16, "participant_id")
        _check_uint_opt(self.isn, 32, "isn")
        if self.tcp_role is not None:
            _check_uint(int(self.tcp_role), 8, "tcp_role")
            if not isinstance(self.tcp_role, TcpRole):
                # Advisory: an undefined value is carried as-is, not rejected.
                with contextlib.suppress(ValueError):
                    object.__setattr__(self, "tcp_role", TcpRole(self.tcp_role))
        object.__setattr__(self, "endpoints", tuple(self.endpoints))
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    @property
    def endpoint(self) -> str | None:
        """The innermost endpoint — where the participant speaks the protocol."""
        return self.endpoints[-1] if self.endpoints else None

    def _encode(self) -> bytes:
        body = _PARTICIPANT_BODY.pack(self.session_id, self.participant_id, 0)
        return body + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        session_id, participant_id, _reserved = cls._body(content, _PARTICIPANT_BODY)
        values, extras = _parse_options(content[_PARTICIPANT_BODY.size :], cls._SPEC.by_id)
        return cls(
            session_id=session_id,
            participant_id=participant_id,
            extra_options=extras,
            **values,
        )


_SESSION_END_BODY = struct.Struct("<Q")


@dataclass(frozen=True)
class SessionEnd(Block):
    """Session End block (``0x12``) — this file holds nothing more for the session.

    Attributes:
        session_id: The session being ended.
        reason: How the session ended (open vocabulary: ``"fin"``, ``"rst"``,
            ``"timeout"``, ``"capture-end"``, …).
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_SESSION_END

    session_id: int
    reason: str | None = None
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_SESSION_END_REASON, "reason", _unpack_str, _pack_str),
    )

    def __post_init__(self) -> None:
        _check_uint(self.session_id, 64, "session_id")
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    def _encode(self) -> bytes:
        return _SESSION_END_BODY.pack(self.session_id) + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        (session_id,) = cls._body(content, _SESSION_END_BODY)
        values, extras = _parse_options(content[_SESSION_END_BODY.size :], cls._SPEC.by_id)
        return cls(session_id=session_id, extra_options=extras, **values)


_RECORD_BODY = struct.Struct("<QHHqHHI")


@dataclass(frozen=True)
class Record(Block):
    """Record block (``0x20``) — one directed payload unit.

    A record is *decoded* iff it carries a ``decoder_id``; a byte-run record
    (raw or pass-through) carries none. :meth:`content` reads the payload as
    the record's ``content_type`` says.

    Attributes:
        session_id: The session this record belongs to.
        sender_pid: Sender participant; recipients are implicit.
        source_id: The Source these bytes came from.
        timestamp: Packet time in ``tick_hz`` ticks (signed); for reassembled
            payloads, the time of the last contributing packet.
        payload: The raw payload bytes (may be empty, e.g. a pure-ACK record).
        flags: Record flags (:class:`RecordFlags`).
        seq_start: Absolute TCP sequence number of the first payload byte.
        ack: The acknowledgement number from the wire.
        ts_first: Optional packet time of the first contributing packet.
        spans: Source ranges these bytes were built from.
        decoder_id: The decoder that produced this record (decoded records only).
        content_type: What the payload is: ``mime:``/``prim:``/``dec:`` label.
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_RECORD

    session_id: int
    sender_pid: int
    source_id: int
    timestamp: int
    payload: bytes = b""
    flags: RecordFlags = RecordFlags(0)
    seq_start: int | None = None
    ack: int | None = None
    ts_first: int | None = None
    spans: tuple[Span, ...] = ()
    decoder_id: int | None = None
    content_type: str | None = None
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_SEQ_START, "seq_start", _unpack_u32, _pack_u32),
        _OptSpec(_frame.OPT_ACK, "ack", _unpack_u32, _pack_u32),
        _OptSpec(_frame.OPT_TS_FIRST, "ts_first", _unpack_i64, _pack_i64),
        _OptSpec(_frame.OPT_SPANS, "spans", _unpack_spans_chunk, _pack_never, mode="concat"),
        _OptSpec(_frame.OPT_DECODER_ID, "decoder_id", _unpack_u16, _pack_u16),
        _OptSpec(_frame.OPT_CONTENT_TYPE, "content_type", _unpack_str, _pack_str),
    )

    def __post_init__(self) -> None:
        _check_uint(self.session_id, 64, "session_id")
        _check_uint(self.sender_pid, 16, "sender_pid")
        _check_uint(self.source_id, 16, "source_id")
        _check_i64(self.timestamp, "timestamp")
        _check_uint(int(self.flags), 16, "flags")
        _check_uint_opt(self.seq_start, 32, "seq_start")
        _check_uint_opt(self.ack, 32, "ack")
        _check_i64_opt(self.ts_first, "ts_first")
        _check_uint_opt(self.decoder_id, 16, "decoder_id")
        _check_uint(len(self.payload), 32, "payload_len")
        object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(self, "flags", RecordFlags(self.flags))
        object.__setattr__(self, "spans", tuple(self.spans))
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    def content(self, *, strict: bool = False) -> int | bytes:
        """Return the payload interpreted as ``content_type`` says.

        Only the ``prim:`` scheme is decoded here, because it is the only
        one the format defines completely: little-endian integers, signed
        iff the token starts with ``i``, width equal to ``payload_len``.
        Everything else falls back to :attr:`payload` unchanged, which is
        the specification's own rule — the bytes are the source of truth
        and the label never replaces them.

        The fallback covers a record with no ``content_type``, a
        ``prim:bytes`` payload, a ``mime:``/``dec:``/unknown scheme, and a
        ``prim:`` label the payload contradicts (an illegal token, or a
        width that disagrees — never padded, truncated, or reinterpreted).
        A ``dec:`` token cannot be resolved from a record at all: the
        format namespaces it by the *decoder's name*, and a record knows
        only its ``decoder_id``, so resolving it needs the file.

        Args:
            strict: Raise instead of falling back, so the return value is
                always the label's meaning rather than possibly the raw
                bytes. Interpreting ``prim:bytes`` still succeeds — the
                label asks for the bytes.

        Returns:
            An :class:`int` for a ``prim:`` integer token, otherwise the
            payload bytes.

        Raises:
            ContentError: With ``strict=True``, if the payload could not be
                interpreted as the label claims (also a
                :class:`ValueError`).

        Example:
            >>> record = Record(
            ...     session_id=0, sender_pid=0, source_id=0, timestamp=0,
            ...     payload=(1234).to_bytes(4, "little"),
            ...     content_type="prim:u32",
            ... )
            >>> record.content()
            1234

        """
        if self.content_type is not None:
            parsed = ContentType.parse(self.content_type)
            if parsed.is_prim:
                value = decode_prim(self.payload, parsed.value)
                if value is not None:
                    return value
        if strict:
            raise ContentError(self._uninterpretable())
        return self.payload

    def _uninterpretable(self) -> str:
        """Say why :meth:`content` had to fall back to the raw payload."""
        content_type = self.content_type
        if content_type is None:
            return "record carries no content_type, so its payload has no declared meaning"
        parsed = ContentType.parse(content_type)
        fault = prim_fault(self.payload, parsed.value) if parsed.is_prim else None
        if fault is not None:
            return f"content_type {content_type!r} {fault}"
        return (
            f"content_type {content_type!r} is advisory, not spec-defined: only prim: can be "
            "interpreted from a record alone, so this label needs a caller-supplied handler"
        )

    def _encode(self) -> bytes:
        body = _RECORD_BODY.pack(
            self.session_id,
            self.sender_pid,
            self.source_id,
            self.timestamp,
            0,
            int(self.flags),
            len(self.payload),
        )
        return body + _frame.padded(self.payload) + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        (session_id, sender_pid, source_id, timestamp, _reserved, flags, payload_len) = cls._body(
            content, _RECORD_BODY
        )
        payload_start = _RECORD_BODY.size
        options_start = payload_start + payload_len + _frame.pad4(payload_len)
        if payload_start + payload_len > len(content) or options_start > len(content):
            msg = f"payload_len {payload_len} overruns its block ({len(content)} content bytes)"
            raise StructuralError(msg)
        payload = content[payload_start : payload_start + payload_len]
        values, extras = _parse_options(content[options_start:], cls._SPEC.by_id)
        return cls(
            session_id=session_id,
            sender_pid=sender_pid,
            source_id=source_id,
            timestamp=timestamp,
            payload=payload,
            flags=RecordFlags(flags),
            extra_options=extras,
            **values,
        )


@dataclass(frozen=True)
class Undecoded(Block):
    """Undecoded block (``0x21``) — a region a decode stage did not decode.

    Carries no bytes, only a reference; every body field is read in the
    referenced *input's* id namespace, exactly as a :class:`Span`'s are.

    Attributes:
        source_id: The input Source (``zpf-input``) whose stream the offsets index.
        session_id: Session inside that input.
        participant_id: Participant (stream) inside that input.
        off_start: Logical 0-based stream offset of the region's first byte.
        off_end: One past the region's last byte.
        reason: Why the region is undecoded (``"undecodable"``, ``"tcp-gap"``,
            ``"truncated"``, …).
        decoder_id: Which decoder declined the region.
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_UNDECODED

    source_id: int
    session_id: int
    participant_id: int
    off_start: int
    off_end: int
    reason: str | None = None
    decoder_id: int | None = None
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_DECODER_ID, "decoder_id", _unpack_u16, _pack_u16),
        _OptSpec(_frame.OPT_UNDECODED_REASON, "reason", _unpack_str, _pack_str),
    )

    def __post_init__(self) -> None:
        _check_uint(self.source_id, 16, "source_id")
        _check_uint(self.session_id, 64, "session_id")
        _check_uint(self.participant_id, 16, "participant_id")
        _check_uint(self.off_start, 64, "off_start")
        _check_uint(self.off_end, 64, "off_end")
        _check_uint_opt(self.decoder_id, 16, "decoder_id")
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    def _encode(self) -> bytes:
        body = _frame.SPAN_ENTRY.pack(
            self.source_id, self.participant_id, self.session_id, self.off_start, self.off_end
        )
        return body + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        source_id, participant_id, session_id, off_start, off_end = cls._body(
            content, _frame.SPAN_ENTRY
        )
        values, extras = _parse_options(content[_frame.SPAN_ENTRY_SIZE :], cls._SPEC.by_id)
        return cls(
            source_id=source_id,
            session_id=session_id,
            participant_id=participant_id,
            off_start=off_start,
            off_end=off_end,
            extra_options=extras,
            **values,
        )


_NAME_BODY = struct.Struct("<QHH")


@dataclass(frozen=True)
class NameResolution(Block):
    """Name/Identity Resolution block (``0x30``) — a label attached after the fact.

    Attributes:
        session_id: The session being labelled into.
        participant_id: The participant being labelled.
        label: The human-readable name being assigned.
        kind: Source/kind of the label (``"nick"``, ``"dns"``, ``"tls-sni"``).
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_NAME_RESOLUTION

    session_id: int
    participant_id: int
    label: str | None = None
    kind: str | None = None
    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(
        _COMMENT_SPEC,
        _OptSpec(_frame.OPT_LABEL, "label", _unpack_str, _pack_str),
        _OptSpec(_frame.OPT_LABEL_KIND, "kind", _unpack_str, _pack_str),
    )

    def __post_init__(self) -> None:
        _check_uint(self.session_id, 64, "session_id")
        _check_uint(self.participant_id, 16, "participant_id")
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    def _encode(self) -> bytes:
        body = _NAME_BODY.pack(self.session_id, self.participant_id, 0)
        return body + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        session_id, participant_id, _reserved = cls._body(content, _NAME_BODY)
        values, extras = _parse_options(content[_NAME_BODY.size :], cls._SPEC.by_id)
        return cls(
            session_id=session_id,
            participant_id=participant_id,
            extra_options=extras,
            **values,
        )


_END_BODY = struct.Struct("<I")


@dataclass(frozen=True)
class End(Block):
    """End block (``0x41``) — if present, the last block; marks the file complete.

    Attributes:
        comment: Free-text note.
        extra_options: Preserved unrecognized/duplicate option occurrences.

    """

    block_type: ClassVar[int] = _frame.BT_END

    comment: str | None = None
    extra_options: tuple[RawOption, ...] = ()

    _SPEC: ClassVar[_SpecTable] = _specs(_COMMENT_SPEC)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra_options", tuple(self.extra_options))

    def _encode(self) -> bytes:
        return _END_BODY.pack(_frame.END_MAGIC) + _encode_options(self, self._SPEC.ordered)

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        (end_magic,) = cls._body(content, _END_BODY)
        if end_magic != _frame.END_MAGIC:
            msg = f"End block magic 0x{end_magic:08X} is not ZEND"
            raise SemanticError(msg)
        values, extras = _parse_options(content[_END_BODY.size :], cls._SPEC.by_id)
        return cls(extra_options=extras, **values)


_CUSTOM_BODY = struct.Struct("<IHH")


@dataclass(frozen=True)
class Custom(Block):
    """Custom block (``0xFF``) — vendor/experimental, namespaced by PEN.

    The payload runs to the end of the block and the specification gives it
    no length field, so its length must be a multiple of 4; vendors own
    their internal framing.

    Attributes:
        pen: IANA Private Enterprise Number (vendor namespace).
        subtype: Vendor-defined block subtype.
        payload: Opaque vendor-defined bytes (length a multiple of 4).

    """

    block_type: ClassVar[int] = _frame.BT_CUSTOM

    pen: int
    subtype: int
    payload: bytes = b""

    def __post_init__(self) -> None:
        _check_uint(self.pen, 32, "pen")
        _check_uint(self.subtype, 16, "subtype")
        object.__setattr__(self, "payload", bytes(self.payload))
        if len(self.payload) % 4:
            msg = f"Custom payload length {len(self.payload)} is not a multiple of 4"
            raise EncodeError(msg)

    def _encode(self) -> bytes:
        return _CUSTOM_BODY.pack(self.pen, self.subtype, 0) + self.payload

    @classmethod
    def _parse(cls, content: bytes) -> Self:
        pen, subtype, _reserved = cls._body(content, _CUSTOM_BODY)
        return cls(pen=pen, subtype=subtype, payload=content[_CUSTOM_BODY.size :])


@dataclass(frozen=True)
class UnknownBlock(Block):
    """A block of a type this implementation does not know, preserved verbatim.

    Unknown block types are the specification's extension mechanism: readers
    skip them by length and faithful tools re-emit them unchanged.

    Attributes:
        block_type: The frame's type code.
        content: The block content, byte-for-byte.

    """

    block_type: int  # type: ignore[misc]  # instance field shadows the ClassVar by design
    content: bytes

    def __post_init__(self) -> None:
        _check_uint(self.block_type, 16, "block_type")
        object.__setattr__(self, "content", bytes(self.content))

    def _encode(self) -> bytes:
        return self.content


_BLOCK_CLASSES: dict[int, type[Block]] = {
    cls.block_type: cls
    for cls in (
        FileHeader,
        Source,
        Decoder,
        Session,
        Participant,
        SessionEnd,
        Record,
        Undecoded,
        NameResolution,
        End,
        Custom,
    )
}


def parse_block(block_type: int, content: bytes) -> Block:
    """Parse one block from its frame type code and content bytes.

    Args:
        block_type: The frame's ``type`` field.
        content: The block content (``length`` bytes after the frame).

    Returns:
        The typed block, or an :class:`UnknownBlock` preserving the content
        verbatim when the type code is not recognized.

    Raises:
        StructuralError: If the body or an option overruns the content, or a
            File Header's magic/version/``tick_hz`` is invalid.
        SemanticError: If the content is well-framed but violates the
            specification (e.g. a wrong End-block magic).

    """
    cls = _BLOCK_CLASSES.get(block_type)
    if cls is None:
        return UnknownBlock(block_type=block_type, content=content)
    return cls.from_content(content)

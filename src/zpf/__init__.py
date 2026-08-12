"""A Python implementation of the Zipline Payload Format (``.zpf``).

The Zipline Payload Format stores the payload of network traffic — the bytes
that flow between endpoints once packets have been reassembled into sessions,
plus the metadata needed to consume them. This package implements v0.16 of the
specification, and exactly that version: while the format is in ``0.x`` every
minor is a separate format, so a file stamped with any other version is
rejected rather than misread. :data:`zpf.SPEC_VERSION` is the single source
of truth for which one.

The flat, spec-mirroring layer lives here: typed blocks (:mod:`zpf.blocks`),
the binary container reader/writer (:mod:`zpf.binary`), and the JSON-Lines
projection with lossless converters (:mod:`zpf.jsonl`), all re-exported at
the package top level.
"""

from __future__ import annotations

from importlib.metadata import version as _distribution_version

from zpf._frame import RawOption
from zpf.binary import BlockReader, BlockWriter
from zpf.blocks import (
    REASON_CLASSES,
    SEQUENCED_BASES,
    SPEC_VERSION,
    UNDECODED_REASONS,
    Block,
    Custom,
    Decoder,
    Discontinuity,
    End,
    FileFlags,
    FileHeader,
    InputExtent,
    NameResolution,
    Origin,
    OutputLayer,
    Participant,
    Record,
    RecordFlags,
    Session,
    SessionEnd,
    SessionFlags,
    Source,
    SourceKind,
    Span,
    TcpRole,
    Undecoded,
    UnknownBlock,
    parse_block,
)
from zpf.conformance import ConformanceChecker
from zpf.content import PRIM_WIDTHS, ContentRegistry, ContentType, decode_prim
from zpf.decode import DecodeStage, DecodeStream, Hints, Seam, decode_stage
from zpf.errors import (
    AdvisoryError,
    ContentError,
    Diagnostic,
    EncodeError,
    SemanticError,
    StructuralError,
    TruncatedError,
    ZpfError,
)
from zpf.jsonl import (
    JsonlReader,
    JsonlWriter,
    binary_to_jsonl,
    dumps_block,
    jsonl_to_binary,
    loads_block,
)
from zpf.order import causal_merge, record_end, seq_geq, seq_leq, seq_lt, verify_sequenced
from zpf.reader import FileReader, SessionReader, detect_face, open
from zpf.reassembly import (
    Break,
    Datagram,
    Gap,
    Segment,
    StreamView,
    record_ranges,
    stream_extent,
    stream_layer,
)
from zpf.transform import (
    InputRef,
    check_coverage,
    check_extents,
    check_splice,
    merge_files,
    resolve_spans,
    rewrite_decoded,
)
from zpf.writer import (
    DecoderHandle,
    DerivedInput,
    FileWriter,
    ParticipantHandle,
    SessionWriter,
    SourceHandle,
    create,
    unix_seconds,
)

#: The library's version, read from the installed distribution metadata so
#: that ``pyproject.toml`` states it once and nothing restates it. This is the
#: version of *this package*, not of the format it implements — for that, see
#: :data:`SPEC_VERSION`. Requires zpf to be installed (``pip install -e .``);
#: importing it from an unbuilt source tree raises
#: :exc:`~importlib.metadata.PackageNotFoundError`.
__version__: str = _distribution_version("zpf")

__all__ = [
    "AdvisoryError",
    "Block",
    "Break",
    "BlockReader",
    "BlockWriter",
    "ConformanceChecker",
    "ContentError",
    "ContentRegistry",
    "ContentType",
    "Custom",
    "Datagram",
    "DecodeStage",
    "DecodeStream",
    "Decoder",
    "DecoderHandle",
    "DerivedInput",
    "Diagnostic",
    "Discontinuity",
    "EncodeError",
    "End",
    "FileFlags",
    "FileHeader",
    "FileReader",
    "FileWriter",
    "Gap",
    "Hints",
    "InputExtent",
    "InputRef",
    "JsonlReader",
    "JsonlWriter",
    "NameResolution",
    "Origin",
    "PRIM_WIDTHS",
    "Participant",
    "ParticipantHandle",
    "RawOption",
    "Record",
    "RecordFlags",
    "Segment",
    "Seam",
    "SemanticError",
    "Session",
    "SessionEnd",
    "SessionFlags",
    "SessionReader",
    "SessionWriter",
    "Source",
    "SourceHandle",
    "REASON_CLASSES",
    "SEQUENCED_BASES",
    "SPEC_VERSION",
    "UNDECODED_REASONS",
    "OutputLayer",
    "SourceKind",
    "Span",
    "StreamView",
    "StructuralError",
    "TcpRole",
    "TruncatedError",
    "Undecoded",
    "UnknownBlock",
    "ZpfError",
    "binary_to_jsonl",
    "causal_merge",
    "check_coverage",
    "check_extents",
    "check_splice",
    "create",
    "decode_prim",
    "decode_stage",
    "detect_face",
    "dumps_block",
    "jsonl_to_binary",
    "loads_block",
    "merge_files",
    "open",
    "parse_block",
    "record_end",
    "record_ranges",
    "resolve_spans",
    "rewrite_decoded",
    "seq_geq",
    "seq_leq",
    "seq_lt",
    "stream_extent",
    "stream_layer",
    "unix_seconds",
    "verify_sequenced",
]

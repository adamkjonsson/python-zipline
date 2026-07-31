"""A Python implementation of the Zipline Payload Format (``.zpf``).

The Zipline Payload Format stores the payload of network traffic — the bytes
that flow between endpoints once packets have been reassembled into sessions,
plus the metadata needed to consume them. This package implements v0.9 of the
specification — the version published as "1.0" and later renumbered. The
current specification, 0.10, is a breaking revision that this package does not
implement.

The flat, spec-mirroring layer lives here: typed blocks (:mod:`zpf.blocks`),
the binary container reader/writer (:mod:`zpf.binary`), and the JSON-Lines
projection with lossless converters (:mod:`zpf.jsonl`), all re-exported at
the package top level.
"""

from __future__ import annotations

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
    End,
    FileFlags,
    FileHeader,
    NameResolution,
    Origin,
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
from zpf.decode import DecodeStage, DecodeStream, decode_stage
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
from zpf.reassembly import Datagram, Gap, Segment, StreamView
from zpf.transform import check_coverage, merge_files
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

__version__ = "0.1.0"

__all__ = [
    "AdvisoryError",
    "Block",
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
    "EncodeError",
    "End",
    "FileFlags",
    "FileHeader",
    "FileReader",
    "FileWriter",
    "Gap",
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
    "seq_geq",
    "seq_leq",
    "seq_lt",
    "unix_seconds",
    "verify_sequenced",
]

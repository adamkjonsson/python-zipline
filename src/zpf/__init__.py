"""A Python implementation of the Zipline Payload Format (``.zpf``).

The Zipline Payload Format stores the payload of network traffic — the bytes
that flow between endpoints once packets have been reassembled into sessions,
plus the metadata needed to consume them. This package implements v1.0 of the
specification.

The flat, spec-mirroring layer lives here: typed blocks (:mod:`zpf.blocks`),
the binary container reader/writer (:mod:`zpf.binary`), and the JSON-Lines
projection with lossless converters (:mod:`zpf.jsonl`), all re-exported at
the package top level.
"""

from __future__ import annotations

from zpf._frame import RawOption
from zpf.binary import BlockReader, BlockWriter
from zpf.blocks import (
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
from zpf.errors import (
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
    FileWriter,
    ParticipantHandle,
    SessionWriter,
    SourceHandle,
    create,
)

__version__ = "0.1.0"

__all__ = [
    "Block",
    "BlockReader",
    "BlockWriter",
    "ConformanceChecker",
    "Custom",
    "Datagram",
    "Decoder",
    "DecoderHandle",
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
    "verify_sequenced",
]

"""A Python implementation of the Zipline Payload Format (``.zpf``).

The Zipline Payload Format stores the payload of network traffic — the bytes
that flow between endpoints once packets have been reassembled into sessions,
plus the metadata needed to consume them. This package implements v1.0 of the
specification.

The flat, spec-mirroring layer lives here: typed blocks (:mod:`zpf.blocks`)
and the binary container reader/writer (:mod:`zpf.binary`), all re-exported
at the package top level.
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
from zpf.errors import (
    Diagnostic,
    EncodeError,
    SemanticError,
    StructuralError,
    TruncatedError,
    ZpfError,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "Block",
    "BlockReader",
    "BlockWriter",
    "Custom",
    "Decoder",
    "Diagnostic",
    "EncodeError",
    "End",
    "FileFlags",
    "FileHeader",
    "NameResolution",
    "Origin",
    "Participant",
    "RawOption",
    "Record",
    "RecordFlags",
    "SemanticError",
    "Session",
    "SessionEnd",
    "SessionFlags",
    "Source",
    "SourceKind",
    "Span",
    "StructuralError",
    "TcpRole",
    "TruncatedError",
    "Undecoded",
    "UnknownBlock",
    "ZpfError",
    "parse_block",
]

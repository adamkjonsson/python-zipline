"""Exception hierarchy and reader diagnostics for the zpf package.

The Zipline Payload Format specification splits reader-side error handling
into two tiers: *structural corruption*, where the byte stream itself can no
longer be trusted and the file must be rejected, and *semantic violations*,
where a well-framed block's content breaks a rule and the reader may isolate
the offending unit. Truncation is a third, expected condition (a live or
crashed writer). This module defines one exception type per tier plus the
:class:`Diagnostic` value used to report non-fatal conditions. Within the
semantic tier, :class:`AdvisoryError` marks the violations the specification
tells a reader to note but read past, rather than isolate.
"""

from __future__ import annotations

from dataclasses import dataclass


class ZpfError(Exception):
    """Base class for all errors raised by the zpf package."""


class StructuralError(ZpfError):
    """The byte stream is corrupt and the file must be rejected.

    Raised for the specification's structural tier: a bad or missing magic,
    a File Header absent or not first, an unimplemented ``version_major``,
    ``tick_hz == 0``, a block length that is not a multiple of 4, or a
    ``payload_len``/TLV ``len`` that overruns its block.
    """


class SemanticError(ZpfError):
    """A well-framed block's content violates a specification MUST.

    Readers may isolate the offending block or session instead of rejecting
    the whole file; this exception is raised where the caller asked for
    strict behavior. See :class:`AdvisoryError` for the subset of violations
    a reader must *not* let cost it the block.
    """


class AdvisoryError(SemanticError):
    """A violated writer obligation that leaves the block fully usable.

    Some MUSTs bind the writer only: the specification tells a reader that
    meets the violation to ignore the offending *label* and keep the bytes,
    which are always the source of truth. A ``prim:`` content type whose
    width disagrees with ``payload_len`` is the example — the reader "MUST
    NOT pad, truncate, or reinterpret", so it keeps the record as opaque
    bytes.

    Raising a distinct type lets both readings coexist: writers
    (:func:`zpf.create`, ``check=True`` on the flat writers) still refuse
    such a block, while a lenient :class:`~zpf.reader.FileReader` records a
    ``nonconformant`` :class:`Diagnostic` and hands the record over anyway.
    """


class TruncatedError(ZpfError):
    """The stream ended inside a block (strict mode only).

    Truncation is an expected condition for live or crashed writers; by
    default readers report it via status attributes and diagnostics rather
    than raising.
    """


class EncodeError(ZpfError):
    """A value cannot be represented in the binary encoding.

    Raised at block construction or serialization time: an out-of-range
    integer, an option value exceeding 65 535 bytes, a zero ``tick_hz``, or
    a ``Custom`` payload whose length is not a multiple of 4.
    """


@dataclass(frozen=True)
class Diagnostic:
    """A non-fatal condition noticed while reading a file.

    Attributes:
        offset: File offset (in bytes) where the condition was detected.
        category: Short machine-readable tag, e.g. ``"truncated"`` or
            ``"trailing-bytes"``.
        message: Human-readable description.

    """

    offset: int
    category: str
    message: str

r"""The ``content_type`` label: its grammar, and the normative ``prim:`` decode.

A record's ``content_type`` names what its payload *is*, as ``scheme:value``.
The specification settles exactly one scheme completely — ``prim:``, whose
vocabulary is closed (``u8``…``u64``, ``i8``…``i64``, ``bytes``), whose
integers are little-endian, whose signedness comes from the token's ``u``/``i``
prefix, and whose width MUST equal ``payload_len``. ``mime:`` says only that
the value is an IANA media type; ``dec:`` is a type private to the record's
decoder, meaning whatever that decoder documents. Neither can be turned into
a Python value without going beyond the format, so neither is decoded here.

The governing sentence, which shapes every signature in this module:

    the bytes always stay the source of truth — the label never replaces them.

So two fallback rules are normative, and :func:`decode_prim` implements them
by returning ``None`` rather than raising: an **unknown scheme or token is
opaque** (not an error — the payload is simply bytes), and a ``prim:`` **width
that disagrees with the payload** MUST be treated as unknown, with the reader
forbidden to "pad, truncate, or reinterpret". Callers therefore get the
spec's fallback by construction — the worst they can do is ignore a ``None``.

Nothing here interprets, normalizes, or case-folds a label beyond splitting
it: a label is carried as written, and comparisons are exact.

This module is deliberately dependency-free — it works on :class:`str` and
:class:`bytes`, never on blocks — so the block layer can build on it.

Example:
    >>> ContentType.parse("prim:u32")
    ContentType(scheme='prim', value='u32')
    >>> decode_prim(b"\xd2\x04\x00\x00", "u32")
    1234
    >>> decode_prim(b"\xd2\x04\x00", "u32") is None  # width disagrees
    True

"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

PRIM_WIDTHS: Mapping[str, int] = MappingProxyType({
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "u64": 8,
    "i64": 8,
})
"""The closed ``prim:`` integer vocabulary, token to its required width."""

PRIM_BYTES = "bytes"
"""The one ``prim:`` token that is not an integer: uninterpreted bytes."""


@dataclass(frozen=True)
class ContentType:
    """A parsed ``content_type`` label — its scheme and the rest.

    Attributes:
        scheme: The part before the first colon: ``"prim"``, ``"mime"``,
            ``"dec"``, or any other (an unknown scheme is opaque, not an
            error). Empty when the label carries no colon at all, since
            such a label names no scheme.
        value: Everything after the first colon, unmodified — a ``prim:``
            token, a media type with its parameters, or a decoder-private
            type name. Colons within it are not separators.

    Example:
        >>> ContentType.parse("mime:text/plain; charset=utf-8")
        ContentType(scheme='mime', value='text/plain; charset=utf-8')

    """

    scheme: str
    value: str

    @classmethod
    def parse(cls, label: str) -> ContentType:
        """Split a ``content_type`` label into its scheme and value.

        Total by design: every string parses, because the format's fallback
        for a label a reader does not understand is to treat the payload as
        opaque bytes, never to reject the record.

        Args:
            label: The label as written in the file.

        Returns:
            The parsed label. A label with no colon yields an empty
            ``scheme`` and keeps the whole label as ``value``.

        """
        scheme, colon, value = label.partition(":")
        if not colon:
            return cls(scheme="", value=label)
        return cls(scheme=scheme, value=value)

    @property
    def is_prim(self) -> bool:
        """Whether this label uses the fully spec-defined ``prim:`` scheme."""
        return self.scheme == "prim"


def decode_prim(payload: bytes, token: str) -> int | bytes | None:
    r"""Interpret a payload as a ``prim:`` token says, or report it opaque.

    Fully normative: the integer tokens are little-endian, signed iff the
    token starts with ``i``, and the token's width MUST equal the payload's
    length. A disagreement is not repaired — the specification forbids
    padding, truncating, and reinterpreting — it is reported as opaque.

    Args:
        payload: The record's payload bytes.
        token: A ``prim:`` label's value, e.g. ``"u32"`` or ``"bytes"``.

    Returns:
        An :class:`int` for an integer token whose width matches; the
        ``payload`` itself for ``"bytes"``; ``None`` when the label cannot
        be honoured (unknown token, or a width the payload contradicts),
        which is the caller's cue to fall back to the raw bytes.

    Example:
        >>> decode_prim(b"\xff\xff", "i16")
        -1

    """
    if token == PRIM_BYTES:
        return payload
    width = PRIM_WIDTHS.get(token)
    if width is None or len(payload) != width:
        return None
    return int.from_bytes(payload, "little", signed=token.startswith("i"))

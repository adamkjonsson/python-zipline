"""Serial-number arithmetic for TCP sequence ordering (RFC 1982).

Absolute TCP sequence numbers are 32-bit and wrap, so every comparison of
``seq_start``/``ack`` values — and of a record's computed end — uses
serial-number arithmetic: ``a < b`` iff ``((a - b) mod 2**32)`` has its high
bit set. This is well-defined for any two values within ``2**31`` of each
other, vastly more than any in-flight TCP window, so it never misorders real
traffic. (For values exactly ``2**31`` apart the comparison is inherently
ambiguous — RFC 1982 leaves it undefined — and this implementation returns
True for both orderings.)

The cross-participant merge algorithm builds on these primitives in a later
milestone; the writer-side conformance checks use them today.
"""

from __future__ import annotations

SEQ_SPACE = 1 << 32
"""The size of the TCP sequence-number space."""

_SEQ_HIGH_BIT = 1 << 31


def seq_lt(a: int, b: int) -> bool:
    """Return whether sequence number ``a`` precedes ``b`` (serial arithmetic).

    Args:
        a: A 32-bit sequence position.
        b: A 32-bit sequence position.

    Returns:
        True iff ``((a - b) mod 2**32)`` has its high bit set — i.e. ``a``
        is behind ``b`` in the wrapped sequence space.

    """
    return a != b and (a - b) % SEQ_SPACE >= _SEQ_HIGH_BIT


def seq_leq(a: int, b: int) -> bool:
    """Return whether ``a`` equals or precedes ``b`` in the sequence space."""
    return a == b or seq_lt(a, b)


def record_end(seq_start: int, payload_len: int) -> int:
    """Return one past a record's last byte: ``seq_start + payload_len`` mod 2**32.

    This is the value a peer's cumulative ``ack`` is compared against; it is
    never stored in a file. A zero-length record's end is its ``seq_start``.
    """
    return (seq_start + payload_len) % SEQ_SPACE

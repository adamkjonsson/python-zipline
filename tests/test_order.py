"""Tests for RFC 1982 serial-number arithmetic over the TCP sequence space."""

from __future__ import annotations

from zpf import record_end, seq_leq, seq_lt


def test_plain_ordering():
    assert seq_lt(1000, 1019)
    assert not seq_lt(1019, 1000)
    assert seq_leq(1000, 1019)
    assert not seq_leq(1019, 1000)


def test_equality():
    assert not seq_lt(5000, 5000)
    assert seq_leq(5000, 5000)


def test_wrap_around():
    assert seq_lt(0xFFFF_FFF0, 0x10)  # crosses the wrap: FFFF_FFF0 precedes 10
    assert not seq_lt(0x10, 0xFFFF_FFF0)
    assert seq_leq(0xFFFF_FFF0, 0x10)


def test_half_space_is_ambiguous_both_ways():
    # RFC 1982 leaves values exactly 2**31 apart undefined; we return True
    # for both orderings (documented).
    assert seq_lt(0, 1 << 31)
    assert seq_lt(1 << 31, 0)


def test_record_end():
    assert record_end(1001, 18) == 1019
    assert record_end(1001, 0) == 1001  # a pure-ACK record's end is its start
    assert record_end(0xFFFF_FFFE, 4) == 2  # wraps

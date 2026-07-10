"""Tests for serial arithmetic, the streaming causal merge, and verification."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import zpf
from zpf import causal_merge, record_end, seq_geq, seq_leq, seq_lt, verify_sequenced


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


def test_seq_geq():
    assert seq_geq(1019, 1000)
    assert seq_geq(1000, 1000)
    assert not seq_geq(1000, 1019)
    assert seq_geq(0x10, 0xFFFF_FFF0)  # wrap


# --- The streaming causal merge -------------------------------------------------


def rec(pid: int, ts: int, payload: bytes = b"", seq: int | None = None,
        ack: int | None = None) -> zpf.Record:
    return zpf.Record(
        session_id=7, sender_pid=pid, source_id=0, timestamp=ts,
        payload=payload, seq_start=seq, ack=ack,
    )


def merged(*streams: list[zpf.Record]) -> list[zpf.Record]:
    return list(causal_merge(dict(enumerate(streams))))


def test_the_specs_skewed_worked_example():
    # Client ISN 1000, server ISN 5000; the server's clock is behind, so its
    # response (ts 995) is stamped before the request (ts 1000) it answers.
    request = rec(0, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n", seq=1001, ack=5001)
    response = rec(1, ts=995, payload=b"HTTP/1.1 200 OK\r\n...", seq=5001, ack=1019)
    assert merged([request], [response]) == [request, response]


def test_pure_ack_records_carry_their_edge():
    data = rec(0, ts=10, payload=b"hello", seq=1)
    pure_ack = rec(1, ts=5, payload=b"", seq=500, ack=6)  # acks all of `data`
    assert merged([data], [pure_ack]) == [data, pure_ack]


def test_syn_handshake_records_order_first():
    syn = rec(0, ts=1, payload=b"", seq=1001)          # SYN: end == start == isn+1
    syn_ack = rec(1, ts=2, payload=b"", seq=5001, ack=1001)
    data = rec(0, ts=3, payload=b"GET", seq=1001, ack=5001)
    assert merged([syn, data], [syn_ack]) == [syn, syn_ack, data]


def test_wrap_around_conversation():
    a1 = rec(0, ts=1, payload=b"\x00" * 0x20, seq=0xFFFF_FFF0)  # wraps to 0x10
    b1 = rec(1, ts=0, payload=b"ok", seq=100, ack=0x10)         # acks the wrapped end
    assert merged([a1], [b1]) == [a1, b1]


def test_concurrent_records_tie_break_deterministically():
    # No acks connect the two directions: order is (timestamp, pid).
    a = [rec(0, ts=10, payload=b"a1", seq=1), rec(0, ts=30, payload=b"a2", seq=3)]
    b = [rec(1, ts=10, payload=b"b1", seq=100), rec(1, ts=20, payload=b"b2", seq=102)]
    out = merged(a, b)
    assert [r.payload for r in out] == [b"a1", b"b1", b"b2", b"a2"]


def test_single_participant_passes_through():
    stream = [rec(0, ts=3, payload=b"x", seq=1), rec(0, ts=1, payload=b"y", seq=2)]
    assert merged(stream) == stream  # stored order, even with a ts inversion


def test_exhausted_peer_unblocks_everything():
    a = [rec(0, ts=1, payload=b"a", seq=1, ack=999)]  # acks bytes B never sent
    assert merged(a, []) == a


def test_hintless_chat_merges_by_timestamp():
    alice = [rec(0, ts=2000, payload=b"hi, all!")]
    bob = [rec(1, ts=2150, payload=b"morning")]
    carol = [rec(2, ts=2100, payload=b"hey alice")]
    out = list(causal_merge({0: alice, 1: bob, 2: carol}))
    assert [r.timestamp for r in out] == [2000, 2100, 2150]


def test_three_participants_with_hints_fall_back_to_timestamps():
    # Ack semantics are pairwise only; with 3 senders the hints are ignored.
    a = [rec(0, ts=3, payload=b"a", seq=1, ack=999_999)]
    b = [rec(1, ts=1, payload=b"b", seq=1)]
    c = [rec(2, ts=2, payload=b"c", seq=1)]
    out = list(causal_merge({0: a, 1: b, 2: c}))
    assert [r.timestamp for r in out] == [1, 2, 3]


def test_stall_raises_naming_both_records():
    a = [rec(0, ts=1, payload=b"a", seq=1, ack=200)]   # needs B's [100, 200)
    b = [rec(1, ts=2, payload=b"b", seq=100, ack=10)]  # needs A's [1, 10)
    with pytest.raises(zpf.SemanticError, match="stalled") as info:
        merged(a, b)
    assert "ack 200" in str(info.value)
    assert "ack 10" in str(info.value)


def test_out_of_order_input_stream_is_rejected():
    a = [rec(0, ts=1, payload=b"x", seq=100), rec(0, ts=2, payload=b"y", seq=50)]
    with pytest.raises(zpf.SemanticError, match="stored-order"):
        merged(a, [])


def test_non_monotone_hintless_stream_raises_with_guidance():
    alice = [rec(0, ts=100, payload=b"x"), rec(0, ts=50, payload=b"y")]
    with pytest.raises(zpf.SemanticError, match="sort the"):
        list(causal_merge({0: alice, 1: [], 2: []}))


# --- verify_sequenced -------------------------------------------------------------


def test_verify_accepts_a_causal_order_and_rejects_a_swap():
    request = rec(0, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n", seq=1001, ack=5001)
    response = rec(1, ts=995, payload=b"HTTP/1.1 200 OK\r\n...", seq=5001, ack=1019)
    verify_sequenced([request, response], participant_count=2)
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([response, request], participant_count=2)


def test_verify_checks_hintless_timestamp_order():
    verify_sequenced([rec(0, ts=1), rec(1, ts=2), rec(0, ts=2)])
    with pytest.raises(zpf.SemanticError, match="timestamp order"):
        verify_sequenced([rec(0, ts=2), rec(1, ts=1)])


def test_verify_checks_per_participant_seq_order():
    with pytest.raises(zpf.SemanticError, match="per-participant"):
        verify_sequenced([rec(0, ts=1, payload=b"x", seq=100), rec(0, ts=2, payload=b"y", seq=50)])


def test_verify_drops_ack_checks_past_two_senders_when_count_unknown():
    # An order that would violate pairwise ack rules is tolerated once a
    # third sender shows the session is not two-party.
    third_first = [
        rec(2, ts=0, payload=b"?", seq=900),
        rec(0, ts=1, payload=b"a", seq=1, ack=200),
        rec(1, ts=2, payload=b"b", seq=100),
    ]
    verify_sequenced(third_first, participant_count=None)


# --- Merge soundness, machine-checked ----------------------------------------------


@st.composite
def tcp_conversation(draw: st.DrawFn) -> tuple[list[zpf.Record], list[zpf.Record]]:
    """Draw a valid two-sided conversation: random turns, correct cumulative acks."""
    isn = (draw(st.integers(0, 2**32 - 1)), draw(st.integers(0, 2**32 - 1)))
    skew = (0, draw(st.integers(-1000, 1000)))  # side B's clock offset
    turns = draw(st.lists(st.tuples(st.integers(0, 1), st.integers(1, 40)), max_size=12))
    sent = [isn[0] + 1, isn[1] + 1]  # next seq per side
    clock = 0
    streams: tuple[list[zpf.Record], list[zpf.Record]] = ([], [])
    for side, size in turns:
        clock += draw(st.integers(1, 10))
        peer = 1 - side
        streams[side].append(
            rec(side, ts=clock + skew[side], payload=b"x" * size,
                seq=sent[side] % 2**32, ack=sent[peer] % 2**32)
        )
        sent[side] += size
    return streams


@given(tcp_conversation())
def test_merge_output_always_verifies(streams: tuple[list[zpf.Record], list[zpf.Record]]):
    out = list(causal_merge({0: streams[0], 1: streams[1]}))
    assert sorted(out, key=id) == sorted(streams[0] + streams[1], key=id)
    verify_sequenced(out, participant_count=2)

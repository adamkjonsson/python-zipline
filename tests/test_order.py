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
        ack: int | None = None, flags: int = 0) -> zpf.Record:
    return zpf.Record(
        session_id=7, sender_pid=pid, source_id=0, timestamp=ts,
        payload=payload, seq_start=seq, ack=ack, flags=flags,
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


def test_a_hintless_stream_may_step_backwards_in_time():
    # Timestamps are not an ordering invariant. The merge is stable with
    # respect to stored order, so one participant's own records keep the
    # order they were stored in however their stamps run.
    alice = [rec(0, ts=100, payload=b"x"), rec(0, ts=50, payload=b"y")]
    merged = list(causal_merge({0: alice, 1: [], 2: []}))
    assert [r.payload for r in merged] == [b"x", b"y"]


def test_the_tie_break_orders_between_participants_by_pid():
    # participant_id is unique within its session, so (timestamp, pid) is a
    # total order over the frontiers and every reader agrees on the result.
    streams = {
        1: [rec(1, ts=10, payload=b"b")],
        0: [rec(0, ts=10, payload=b"a")],
        2: [rec(2, ts=10, payload=b"c")],
    }
    assert [r.payload for r in causal_merge(streams)] == [b"a", b"b", b"c"]


# --- verify_sequenced -------------------------------------------------------------


def test_verify_accepts_a_causal_order_and_rejects_a_swap():
    request = rec(0, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n", seq=1001, ack=5001)
    response = rec(1, ts=995, payload=b"HTTP/1.1 200 OK\r\n...", seq=5001, ack=1019)
    verify_sequenced([request, response], participant_count=2)
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([response, request], participant_count=2)


def test_verify_does_not_check_timestamp_order():
    # A sequenced hint-less order may rest on a protocol sequence or an
    # out-of-band record — neither of which the clock reflects — so stored
    # order is authoritative and a backwards stamp is not a violation.
    verify_sequenced([rec(0, ts=1), rec(1, ts=2), rec(0, ts=2)])
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


# --- Acks are positions in the peer's offset space (0.22, #75) --------------------

GIB = 1 << 30


def upload_past_2gib(*, sequenced: bool) -> bytes:
    """#75's repro: stream-past-2gib's client, a server that only acks its SYN."""
    import io

    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000, produced_by="repro") as w:
        w.add_source("capture", uri="upload.pcap", link_type=1)
        dec = w.add_decoder("repro", output_layer=zpf.OutputLayer.TRANSPORT)
        s = w.begin_session(proto="tcp", key="10.0.0.1:51000 <-> 10.0.0.2:80",
                            sequenced=sequenced, linearize=sequenced)
        a = s.participant("10.0.0.1:51000", isn=1000, tcp_role=zpf.TcpRole.INITIATOR)
        b = s.participant("10.0.0.2:80", isn=5000, tcp_role=zpf.TcpRole.RESPONDER)
        s.record(a, ts=0, payload=b"", flags=zpf.RecordFlags.SYN,
                 hints=zpf.Hints(seq_start=1001), decoded=zpf.Decoded(decoder=dec))
        s.record(b, ts=1, payload=b"", flags=zpf.RecordFlags.SYN,
                 hints=zpf.Hints(seq_start=5001, ack=1001), decoded=zpf.Decoded(decoder=dec))
        for k in range(4):  # 0, 1, 2, 3 GiB
            s.record(a, ts=2 + k, payload=b"kkkk", flags=zpf.RecordFlags.PSH,
                     hints=zpf.Hints(seq_start=1001 + k * GIB, ack=5001),
                     decoded=zpf.Decoded(decoder=dec))
        s.end(reason="capture-end")
    return sink.getvalue()


def test_a_sequenced_upload_past_2gib_with_a_pure_ack_peer_writes_and_verifies():
    """#75, verbatim: refused at the 2 GiB record by 0.5.x, at write and at read."""
    import io

    with zpf.open(io.BytesIO(upload_past_2gib(sequenced=True))) as reader:
        (session,) = reader.sessions()
        session.verify()
        records = list(session.records())
    assert [r.seq_start for r in records[2:]] == [1001 + k * GIB for k in range(4)]


def test_a_real_violation_past_2gib_is_still_caught():
    # The server's acks keep up, so its last one is a position 2 GiB + 8 into
    # the client's stream; the client record ending there is stored after it.
    isns = {0: 1000, 1: 5000}
    syn = rec(0, ts=0, seq=1001)
    syn_ack = rec(1, ts=1, seq=5001, ack=1001)
    data = [rec(0, ts=2 + k, payload=b"kkkkkkkk", seq=1001 + k * GIB) for k in range(3)]
    acks = [rec(1, ts=10 + k, seq=5001, ack=1001 + k * GIB + 8) for k in (1, 2)]
    verify_sequenced([syn, syn_ack, *data, *acks], participant_count=2, isns=isns)
    with pytest.raises(zpf.SemanticError, match="already acknowledged") as info:
        verify_sequenced([syn, syn_ack, data[0], data[1], *acks, data[2]],
                         participant_count=2, isns=isns)
    assert f"offset {2 * GIB + 8}" in str(info.value)


def test_acks_that_retreat_in_stored_order_are_walked_not_refused():
    # Stored order is seq_start order: the retransmitted segment A, stored
    # first, was sent last and acks more than B. Positions step back, and the
    # check still holds the highest one against what follows.
    s1 = rec(1, ts=1, payload=b"s" * 50, seq=5001)
    s2 = rec(1, ts=2, payload=b"t" * 50, seq=5051)
    a = rec(0, ts=4, payload=b"a" * 10, seq=1001, ack=5101)  # all of s1 and s2
    b = rec(0, ts=3, payload=b"b" * 10, seq=1011, ack=5051)  # s1 only
    isns = {0: 1000, 1: 5000}
    verify_sequenced([s1, s2, a, b], participant_count=2, isns=isns)
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([s1, a, b, s2], participant_count=2, isns=isns)


def test_an_ack_below_the_peers_first_captured_byte_is_a_negative_position():
    # Mid-stream, no isn: the response acks 8 client bytes sent before the
    # capture began. Its position is -8, below every client record's end;
    # stored unsigned it would wrap and refuse this correct order.
    response = rec(1, ts=1000, payload=b"HTTP/1.1 200", seq=5001, ack=993)
    request = rec(0, ts=1100, payload=b"GET /two", seq=1001, ack=5013)
    verify_sequenced([response, request], participant_count=2)
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([request, response], participant_count=2)


def test_an_unplaceable_record_is_still_placed_by_its_ack():
    # No seq_start, so no end and no edge runs from it; its ack still
    # places it after the peer bytes that ack covers.
    acker = rec(0, ts=1, ack=5011)
    server = rec(1, ts=2, payload=b"0123456789", seq=5001)
    verify_sequenced([server, acker], participant_count=2)
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([acker, server], participant_count=2)


def test_the_merge_compares_positions_past_2gib():
    # The SYN-ACK acks only the client's SYN, so nothing holds it back and
    # its timestamp places it between the 2 GiB and 3 GiB records. Read
    # serially, the 3 GiB record's end looked *below* ack 1001, so the merge
    # held the SYN-ACK back until the client stream was exhausted.
    client = [rec(0, ts=1, seq=1001)] + [
        rec(0, ts=2 + k + (k == 3), payload=b"kkkkkkkk", seq=1001 + k * GIB)
        for k in range(4)
    ]
    server = [rec(1, ts=5, seq=5001, ack=1001)]
    out = list(causal_merge({0: client, 1: server}, isns={0: 1000, 1: 5000}))
    assert [(r.sender_pid, r.timestamp) for r in out] == [
        (0, 1), (0, 2), (0, 3), (0, 4), (1, 5), (0, 6),
    ]


def test_the_merge_holds_a_record_behind_an_unplaceable_peer_head():
    # The server's first record has no seq_start, so it bounds nothing, but
    # the record behind it ends inside the client's ack; the client record
    # must wait for both rather than overtake them on its timestamp.
    # Through 0.5.x an unplaceable head held nothing back, and the client
    # record's earlier timestamp put it first.
    server = [rec(1, ts=3), rec(1, ts=4, payload=b"0123456789", seq=5001)]
    client = [rec(0, ts=2, payload=b"x", seq=1001, ack=5011)]
    out = list(causal_merge({0: client, 1: server}, isns={0: 1000, 1: 5000}))
    assert [(r.sender_pid, r.timestamp) for r in out] == [(1, 3), (1, 4), (0, 2)]



# --- A record's end is the sequence numbers it occupies (0.23, zipline#160) --------


def handshake_data_pure_ack() -> list[zpf.Record]:
    """Build the ordinary trace: handshake, client data, a stored server pure ACK."""
    return [
        rec(0, ts=1, seq=1001, flags=zpf.RecordFlags.SYN),
        rec(1, ts=2, seq=5001, ack=1001, flags=zpf.RecordFlags.SYN),
        rec(0, ts=3, payload=b"GET /\r\n\r", seq=1001, ack=5001),
        rec(1, ts=4, seq=5001, ack=1009),  # a pure ACK: no payload, no flag
    ]


def test_a_stored_pure_ack_draws_no_edge():
    """zipline#160: a pure ACK occupies no sequence number, so it has no end.

    Through 0.22 it ended at its offset, 0, which the client's ack 5001
    reached, so it drew an edge backwards to the data it answers: a cycle,
    refused when sequenced and stalled when merged.
    """
    trace = handshake_data_pure_ack()
    isns = {0: 1000, 1: 5000}
    verify_sequenced(trace, participant_count=2, isns=isns)
    streams = {0: [trace[0], trace[2]], 1: [trace[1], trace[3]]}
    out = list(causal_merge(streams, isns=isns))
    assert [r.timestamp for r in out] == [1, 2, 3, 4]


def test_a_pure_ack_is_still_ordered_by_its_own_ack():
    # The edges *into* a pure ACK stand: it must follow the data it acks.
    syn, syn_ack, data, pure_ack = handshake_data_pure_ack()
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([syn, syn_ack, pure_ack, data], participant_count=2,
                         isns={0: 1000, 1: 5000})


def test_a_zero_length_fin_ends_one_past_its_offset():
    # The FIN occupies the number at its offset (8), so an ack of 1009
    # (position 8) acknowledges the data, not the FIN, and an ack of 1010 does.
    isns = {0: 1000, 1: 5000}
    syn, syn_ack, data, _ = handshake_data_pure_ack()
    fin = rec(0, ts=5, seq=1009, ack=5001, flags=zpf.RecordFlags.FIN)
    acks_data = rec(1, ts=4, seq=5001, ack=1009)
    acks_fin = rec(1, ts=6, seq=5001, ack=1010)
    verify_sequenced([syn, syn_ack, data, acks_data, fin, acks_fin],
                     participant_count=2, isns=isns)
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([syn, syn_ack, data, acks_fin, fin],
                         participant_count=2, isns=isns)


def test_a_data_record_carrying_fin_keeps_the_end_of_its_data():
    # An ack covering the data proves the segment was sent, FIN or not.
    isns = {0: 1000, 1: 5000}
    syn, syn_ack, _, _ = handshake_data_pure_ack()
    data_fin = rec(0, ts=3, payload=b"GET /\r\n\r", seq=1001, ack=5001,
                   flags=zpf.RecordFlags.FIN)
    acks_data = rec(1, ts=4, seq=5001, ack=1009)
    with pytest.raises(zpf.SemanticError, match="already acknowledged"):
        verify_sequenced([syn, syn_ack, acks_data, data_fin], participant_count=2, isns=isns)


def test_the_merge_does_not_wait_on_a_pure_ack_for_an_edge_it_cannot_draw():
    # The data acks position 0 of the server's stream; the server's next
    # record is a pure ACK at offset 0, which has no end and so cannot be
    # what that ack covers. Phase 3 read it as ending at 0 and held the data
    # back for it, while the pure ACK waited for the data: a stall.
    trace = handshake_data_pure_ack()
    streams = {0: [trace[0], trace[2]], 1: [trace[1], trace[3]]}
    out = list(causal_merge(streams, isns={0: 1000, 1: 5000}))
    assert [(r.sender_pid, r.timestamp) for r in out] == [(0, 1), (1, 2), (0, 3), (1, 4)]

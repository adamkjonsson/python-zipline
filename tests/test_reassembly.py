"""Tests for the reassembly views (SessionReader.reassemble)."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest

import zpf

if TYPE_CHECKING:
    from collections.abc import Callable

    from zpf.writer import SessionWriter

SEQ_SPACE = 1 << 32


def build(fill: Callable[[SessionWriter], None], *, proto: str = "tcp") -> zpf.FileReader:
    """Write a one-session file with ``fill``, reopen it, and return the reader."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000) as writer:
        writer.add_source("capture")
        with writer.begin_session(proto=proto, session_id=7) as session:
            fill(session)
    return zpf.open(io.BytesIO(sink.getvalue()))


def only_view(fill: Callable[[SessionWriter], None], **kwargs: str) -> zpf.StreamView:
    """Build a single-participant session and return its one stream view.

    The reader is left open (the returned view keeps it alive through its
    record factory, and is read lazily from the in-memory buffer).
    """
    reader = build(fill, **kwargs)
    (view,) = reader.session(7).reassemble()
    return view


# --- Stream-oriented: coalescing and offsets -----------------------------------------


def test_contiguous_records_coalesce_into_one_segment():
    def fill(s: zpf.SessionWriter) -> None:
        client = s.participant("10.0.0.1:51000", isn=1000)
        s.record(client, ts=1, payload=b"hello", seq_start=1001)
        s.record(client, ts=2, payload=b"world", seq_start=1006)

    view = only_view(fill)
    assert view.is_stream_oriented
    assert view.off_start == 0
    (segment,) = view.segments()
    assert segment == zpf.Segment(data=b"helloworld", off_start=0, off_end=10, ts=2)
    assert view.reassembled() == b"helloworld"
    assert list(view.chunks()) == [segment]


def test_segment_ts_is_the_last_contributing_timestamp():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a", isn=1000)
        s.record(p, ts=5, payload=b"AAAA", seq_start=1001)
        s.record(p, ts=9, payload=b"BBBB", seq_start=1005)

    (segment,) = only_view(fill).segments()
    assert segment.ts == 9


# --- Stream-oriented: gaps ------------------------------------------------------------


def test_interior_gap_is_surfaced_between_segments():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a", isn=1000)
        s.record(p, ts=1, payload=b"AAAA", seq_start=1001)  # off 0..4
        s.record(p, ts=2, payload=b"CCCC", seq_start=1009)  # off 8..12, gap 4..8

    view = only_view(fill)
    assert list(view.chunks()) == [
        zpf.Segment(data=b"AAAA", off_start=0, off_end=4, ts=1),
        zpf.Gap(off_start=4, off_end=8),
        zpf.Segment(data=b"CCCC", off_start=8, off_end=12, ts=2),
    ]
    assert [s.data for s in view.segments()] == [b"AAAA", b"CCCC"]


def test_reassembled_raises_on_a_gap():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a", isn=1000)
        s.record(p, ts=1, payload=b"AAAA", seq_start=1001)
        s.record(p, ts=2, payload=b"CCCC", seq_start=1009)

    with pytest.raises(zpf.ZpfError, match="gap"):
        only_view(fill).reassembled()


def test_leading_gap_when_first_byte_is_past_the_origin():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a", isn=1000)  # origin = 1001
        s.record(p, ts=1, payload=b"DDDD", seq_start=1005)  # off 4..8

    view = only_view(fill)
    assert view.off_start == 4
    assert list(view.chunks()) == [
        zpf.Gap(off_start=0, off_end=4),
        zpf.Segment(data=b"DDDD", off_start=4, off_end=8, ts=1),
    ]


def test_no_isn_anchors_the_origin_at_the_first_byte():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a")  # handshake missed: no isn
        s.record(p, ts=1, payload=b"EEEE", seq_start=5000)
        s.record(p, ts=2, payload=b"FFFF", seq_start=5004)

    view = only_view(fill)
    assert view.is_stream_oriented  # seq_start hints make it a stream
    assert view.off_start == 0  # no leading gap without an isn
    (segment,) = view.segments()
    assert segment == zpf.Segment(data=b"EEEEFFFF", off_start=0, off_end=8, ts=2)


def test_sequence_space_wrap_stays_contiguous():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a", isn=SEQ_SPACE - 3)  # origin = 2**32 - 2
        s.record(p, ts=1, payload=b"AA", seq_start=SEQ_SPACE - 2)  # off 0..2, wraps to 0
        s.record(p, ts=2, payload=b"BB", seq_start=0)  # off 2..4

    (segment,) = only_view(fill).segments()
    assert segment == zpf.Segment(data=b"AABB", off_start=0, off_end=4, ts=2)


# --- Pure-ACK handling ----------------------------------------------------------------


def test_zero_length_records_are_skipped_by_segments_but_kept_by_datagrams():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a", isn=1000)
        s.record(p, ts=1, payload=b"AAAA", seq_start=1001)
        s.record(p, ts=2, payload=b"", seq_start=1005, ack=1)  # pure ACK
        s.record(p, ts=3, payload=b"BBBB", seq_start=1005)

    view = only_view(fill)
    (segment,) = view.segments()
    assert segment == zpf.Segment(data=b"AAAABBBB", off_start=0, off_end=8, ts=3)
    assert [d.data for d in view.datagrams()] == [b"AAAA", b"", b"BBBB"]


# --- Packet-oriented streams ----------------------------------------------------------


def test_packet_oriented_stream_yields_datagrams_with_cumulative_offsets():
    def fill(s: zpf.SessionWriter) -> None:
        feed = s.participant("relay:514")
        s.record(feed, ts=1, payload=b"pkt1")
        s.record(feed, ts=2, payload=b"pkt22")

    view = only_view(fill, proto="udp")
    assert not view.is_stream_oriented
    assert view.off_start == 0
    datagrams = list(view.datagrams())
    assert [(d.data, d.off_start, d.off_end, d.ts) for d in datagrams] == [
        (b"pkt1", 0, 4, 1),
        (b"pkt22", 4, 9, 2),
    ]
    assert [d.record.payload for d in datagrams] == [b"pkt1", b"pkt22"]


@pytest.mark.parametrize("method", ["segments", "chunks", "reassembled"])
def test_stream_methods_reject_a_packet_oriented_stream(method: str):
    def fill(s: zpf.SessionWriter) -> None:
        feed = s.participant("relay:514")
        s.record(feed, ts=1, payload=b"pkt1")

    view = only_view(fill, proto="udp")
    with pytest.raises(zpf.ZpfError, match="datagrams"):
        if method == "reassembled":
            view.reassembled()
        else:
            list(getattr(view, method)())


# --- reassemble() over a whole session ------------------------------------------------


def test_reassemble_returns_one_view_per_participant_in_order():
    def fill(s: zpf.SessionWriter) -> None:
        client = s.participant("10.0.0.1:51000", isn=1000)
        server = s.participant("93.184.216.34:80", isn=5000)
        s.record(client, ts=1, payload=b"GET", seq_start=1001)
        s.record(server, ts=2, payload=b"200", seq_start=5001, ack=1004)

    with build(fill) as reader:
        views = reader.session(7).reassemble()
        assert [v.participant.endpoint for v in views] == [
            "10.0.0.1:51000",
            "93.184.216.34:80",
        ]
        assert [v.reassembled() for v in views] == [b"GET", b"200"]


def test_views_iterate_independently():
    def fill(s: zpf.SessionWriter) -> None:
        client = s.participant("a", isn=1000)
        server = s.participant("b", isn=5000)
        s.record(client, ts=1, payload=b"c1", seq_start=1001)
        s.record(client, ts=3, payload=b"c2", seq_start=1003)
        s.record(server, ts=2, payload=b"s1", seq_start=5001, ack=1003)

    with build(fill) as reader:
        client_view, server_view = reader.session(7).reassemble()
        client_segments = client_view.segments()
        server_segments = server_view.segments()
        assert next(server_segments).data == b"s1"
        assert next(client_segments).data == b"c1c2"

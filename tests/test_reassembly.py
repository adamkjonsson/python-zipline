"""Tests for the reassembly views (SessionReader.reassemble)."""

from __future__ import annotations

import dataclasses
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


# --- Span helpers ---------------------------------------------------------------------


def cited_stream() -> zpf.StreamView:
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("10.0.0.1:51000", isn=1000)
        s.record(p, ts=1, payload=b"GET /a\r\n", seq_start=1001)
        s.record(p, ts=2, payload=b"GET /b\r\n", seq_start=1009)

    return only_view(fill)


def test_cite_fills_in_the_inputs_ids():
    view = cited_stream()
    assert view.cite(0, 8, source=3) == zpf.Span(
        source_id=3, session_id=7, participant_id=0, off_start=0, off_end=8
    )


def test_cite_accepts_a_source_handle():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1) as writer:
        handle = writer.add_source("zpf-input", uri="raw.zpf")
        span = cited_stream().cite(0, 4, source=handle)
    assert span.source_id == handle.source_id


def test_cited_as_binds_the_source_once():
    view = cited_stream().cited_as(5)
    assert view.cite(0, 8).source_id == 5
    assert view.cite(0, 8, source=9).source_id == 9  # per-call override still wins


def test_cite_without_a_source_explains_why_it_cannot_guess():
    with pytest.raises(zpf.ZpfError, match="citing file's id namespace"):
        cited_stream().cite(0, 8)


def test_cite_rejects_a_non_source():
    with pytest.raises(zpf.ZpfError, match="SourceHandle"):
        cited_stream().cite(0, 8, source="zpf-input")


def test_segment_cite_is_relative_to_the_segment():
    view = cited_stream().cited_as(2)
    (segment,) = view.segments()
    assert segment.data == b"GET /a\r\nGET /b\r\n"
    # The second message sits at segment offset 8; cite() adds the run's start.
    assert segment.cite(8, 16) == zpf.Span(
        source_id=2, session_id=7, participant_id=0, off_start=8, off_end=16
    )


def test_segment_cite_offsets_are_absolute_after_a_gap():
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("a", isn=1000)
        s.record(p, ts=1, payload=b"AAAA", seq_start=1001)  # off 0..4
        s.record(p, ts=2, payload=b"CCCC", seq_start=1009)  # off 8..12

    view = only_view(fill).cited_as(1)
    _, _, second = view.chunks()
    # Local offset 0 of the post-gap run is stream offset 8, not 4.
    assert second.cite(0, 4) == zpf.Span(
        source_id=1, session_id=7, participant_id=0, off_start=8, off_end=12
    )


@pytest.mark.parametrize(("start", "end"), [(0, 17), (-1, 4), (5, 2), (20, 24)])
def test_segment_cite_rejects_a_range_outside_the_run(start: int, end: int):
    (segment,) = cited_stream().cited_as(1).segments()
    with pytest.raises(zpf.ZpfError, match="outside"):
        segment.cite(start, end)


def test_a_hand_built_segment_cannot_cite():
    segment = zpf.Segment(data=b"abc", off_start=0, off_end=3, ts=1)
    with pytest.raises(zpf.ZpfError, match="no stream view"):
        segment.cite(0, 3)


def test_the_view_is_not_part_of_segment_equality():
    (segment,) = cited_stream().segments()
    assert segment == zpf.Segment(
        data=b"GET /a\r\nGET /b\r\n", off_start=0, off_end=16, ts=2
    )
    assert "view" not in repr(segment)


def test_cited_spans_survive_a_write_read_round_trip():
    """Spans minted by cite() are writable and read back unchanged."""
    view = cited_stream()
    sink = io.BytesIO()
    with zpf.create(
        sink, tick_hz=1_000_000, produced_by="t 1.0", produced_at=1_700_000_000
    ) as writer:
        source = writer.add_source("zpf-input", uri="raw.zpf")
        decoder = writer.add_decoder("http/1.1")
        stream = view.cited_as(source)
        (segment,) = stream.segments()
        with writer.begin_session(proto="http", session_id=7) as out:
            handle = out.participant("10.0.0.1:51000")
            out.record(
                handle,
                ts=segment.ts,
                payload=segment.data[:8],
                decoder=decoder,
                content_type="dec:http-request",
                spans=(segment.cite(0, 8),),
            )
    with zpf.open(io.BytesIO(sink.getvalue())) as decoded:
        (record,) = decoded.session(7).records()
        assert record.spans == (
            zpf.Span(
                source_id=source.source_id,
                session_id=7,
                participant_id=0,
                off_start=0,
                off_end=8,
            ),
        )


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


# --- Offset spaces: which rule applies to which layer ------------------------


def test_a_decoded_stream_is_positional_never_hole_inclusive():
    # A decoded stream is the concatenation of that participant's record
    # payloads in stored order. Undecoded regions name ranges in the input's
    # space, so they contribute nothing here and there are no holes to count.
    participant = zpf.Participant(session_id=7, participant_id=0)
    records = [
        zpf.Record(session_id=7, sender_pid=0, source_id=1, timestamp=0,
                   payload=b"REQ", decoder_id=1),
        zpf.Record(session_id=7, sender_pid=0, source_id=1, timestamp=1,
                   payload=b"RESPONSE", decoder_id=1),
    ]
    decoders = {1: zpf.Decoder(decoder_id=1, name="http/1.1")}
    assert zpf.reassembly.stream_layer(records, decoders) is zpf.OutputLayer.DECODED
    assert (
        zpf.reassembly.record_ranges(participant, records, zpf.OutputLayer.DECODED)
    ) == ((0, 3), (3, 11))
    assert zpf.reassembly.stream_extent(participant, records, zpf.OutputLayer.DECODED) == 11


def test_the_layer_decides_the_space_not_the_presence_of_hints():
    # A decoded record carrying transport hints must still be placed
    # positionally: the space belongs to the layer, not to seq_start.
    participant = zpf.Participant(session_id=7, participant_id=0, isn=1000)
    hinted = zpf.Record(session_id=7, sender_pid=0, source_id=1, timestamp=0,
                        payload=b"abcd", seq_start=1101)
    assert (
        zpf.reassembly.record_ranges(participant, [hinted], zpf.OutputLayer.TRANSPORT)
    ) == ((100, 104),)
    decoded = dataclasses.replace(hinted, decoder_id=1)
    assert (
        zpf.reassembly.record_ranges(participant, [decoded], zpf.OutputLayer.DECODED)
    ) == ((0, 4),)


def test_a_transport_stream_counts_its_holes():
    # The hole between the two records occupies offsets no payload covers,
    # and the next delivered byte resumes past it.
    participant = zpf.Participant(session_id=7, participant_id=0, isn=1000)
    records = [
        zpf.Record(session_id=7, sender_pid=0, source_id=1, timestamp=0,
                   payload=b"0123456789", seq_start=1001),
        zpf.Record(session_id=7, sender_pid=0, source_id=1, timestamp=1,
                   payload=b"xyz", seq_start=1050),
    ]
    assert (
        zpf.reassembly.record_ranges(participant, records, zpf.OutputLayer.TRANSPORT)
    ) == ((0, 10), (49, 52))
    assert zpf.reassembly.stream_extent(participant, records, zpf.OutputLayer.TRANSPORT) == 52


# --- Discontinuity in the positional arithmetic (0.14) ------------------------


def _decoded(pid: int, payload: bytes, ts: int = 0) -> zpf.Record:
    return zpf.Record(
        session_id=7, sender_pid=pid, source_id=1, timestamp=ts, payload=payload, decoder_id=1
    )


def test_a_declared_width_displaces_every_later_record():
    """The whole reason the block exists: a skipped width shifts every range.

    Without the break the second record sits at ``[3, 11)``; with a declared
    width of 25 it sits at ``[28, 36)``. A reader that parses the block but
    ignores ``width`` gets the first answer, which is why reading it is not
    enough.
    """
    participant = zpf.Participant(session_id=7, participant_id=0)
    blocks = [
        _decoded(0, b"REQ"),
        zpf.Discontinuity(session_id=7, participant_id=0, width=25, reason="stream-gap"),
        _decoded(0, b"RESPONSE", ts=1),
    ]
    assert (
        zpf.reassembly.record_ranges(participant, blocks, zpf.OutputLayer.DECODED)
    ) == ((0, 3), (28, 36))
    assert zpf.reassembly.stream_extent(participant, blocks, zpf.OutputLayer.DECODED) == 36


def test_an_unknown_width_contributes_nothing_to_the_arithmetic():
    """Absent means unknowable, and an unknowable extent adds 0.

    The records still do not *join* — that is the block's other job, and the
    one a consumer must honour — but there is no number to displace them by.
    """
    participant = zpf.Participant(session_id=7, participant_id=0)
    blocks = [
        _decoded(0, b"REQ"),
        zpf.Discontinuity(session_id=7, participant_id=0, reason="tls-record-lost"),
        _decoded(0, b"RESPONSE", ts=1),
    ]
    assert (
        zpf.reassembly.record_ranges(participant, blocks, zpf.OutputLayer.DECODED)
    ) == ((0, 3), (3, 11))


def test_a_zero_width_break_is_read_as_zero_not_as_unknown():
    # Numerically the same as an unknown width, but reached by a different
    # route: a declared 0 is a zero-width hole. Pinning it keeps a future
    # `width or 0` refactor from quietly conflating the two.
    participant = zpf.Participant(session_id=7, participant_id=0)
    blocks = [
        _decoded(0, b"REQ"),
        zpf.Discontinuity(session_id=7, participant_id=0, width=0),
        _decoded(0, b"RESPONSE", ts=1),
    ]
    assert (
        zpf.reassembly.record_ranges(participant, blocks, zpf.OutputLayer.DECODED)
    ) == ((0, 3), (3, 11))


def test_a_break_after_the_last_record_does_not_extend_the_stream():
    """Nothing follows it to displace, and the bytes are missing by definition.

    A coverage check reads this number as "how far the stream reaches"; a
    trailing break must not make it claim bytes that are not there.
    """
    participant = zpf.Participant(session_id=7, participant_id=0)
    blocks = [
        _decoded(0, b"REQ"),
        zpf.Discontinuity(session_id=7, participant_id=0, width=25),
    ]
    assert zpf.reassembly.record_ranges(participant, blocks, zpf.OutputLayer.DECODED) == ((0, 3),)
    assert zpf.reassembly.stream_extent(participant, blocks, zpf.OutputLayer.DECODED) == 3


def test_a_break_leaves_a_hinted_transport_stream_alone():
    """Transport offsets are absolute, so a width has nothing to add to them.

    The block is defined for decoded layers only; one appearing here is a
    conformance violation, not an instruction to move the records.
    """
    participant = zpf.Participant(session_id=7, participant_id=0, isn=1000)
    blocks = [
        zpf.Record(session_id=7, sender_pid=0, source_id=1, timestamp=0,
                   payload=b"0123456789", seq_start=1001),
        zpf.Discontinuity(session_id=7, participant_id=0, width=25),
        zpf.Record(session_id=7, sender_pid=0, source_id=1, timestamp=1,
                   payload=b"xyz", seq_start=1050),
    ]
    assert (
        zpf.reassembly.record_ranges(participant, blocks, zpf.OutputLayer.TRANSPORT)
    ) == ((0, 10), (49, 52))


def test_units_surface_a_break_that_datagrams_hides():
    """The offsets cannot betray an unknown-width break, so the marker must.

    Both records sit at ``[0,3)`` and ``[3,11)`` either side of it — exactly
    where they would sit with no break at all. A consumer honouring "MUST NOT
    treat the records either side as contiguous" has nothing to go on unless
    the break itself is yielded.
    """
    participant = zpf.Participant(session_id=7, participant_id=0)
    blocks = [
        _decoded(0, b"REQ"),
        zpf.Discontinuity(session_id=7, participant_id=0, reason="tls-record-lost"),
        _decoded(0, b"RESPONSE", ts=1),
    ]
    view = zpf.StreamView(participant, lambda: iter(blocks))
    assert [(d.off_start, d.off_end) for d in view.datagrams()] == [(0, 3), (3, 11)]
    units = list(view.units())
    assert [type(unit).__name__ for unit in units] == ["Datagram", "Break", "Datagram"]
    assert units[1] == zpf.Break(off_start=3, width=None, reason="tls-record-lost")


def test_a_break_reports_the_width_it_declares():
    participant = zpf.Participant(session_id=7, participant_id=0)
    blocks = [
        _decoded(0, b"REQ"),
        zpf.Discontinuity(session_id=7, participant_id=0, width=25),
        _decoded(0, b"RESPONSE", ts=1),
    ]
    view = zpf.StreamView(participant, lambda: iter(blocks))
    units = list(view.units())
    assert units[1] == zpf.Break(off_start=3, width=25, reason=None)
    assert (units[2].off_start, units[2].off_end) == (28, 36)


# --- The layer rule (0.16) ------------------------------------------------------------


def _rec(decoder_id: int | None = None) -> zpf.Record:
    return zpf.Record(
        session_id=7, sender_pid=0, source_id=1, timestamp=0,
        payload=b"x", decoder_id=decoder_id,
    )


def test_a_decoder_less_stream_is_transport():
    assert zpf.reassembly.stream_layer([_rec()], {}) is zpf.OutputLayer.TRANSPORT


def test_a_reassembly_record_carries_a_decoder_id_and_is_still_transport():
    """The trap a 0.14 reader walks into, and the reason the rule has two halves."""
    decoders = {1: zpf.Decoder(decoder_id=1, output_layer=zpf.OutputLayer.TRANSPORT)}
    assert zpf.reassembly.stream_layer([_rec(1)], decoders) is zpf.OutputLayer.TRANSPORT


def test_an_empty_stream_is_transport():
    # No records, so no decoder: the no-decoder answer, and nothing to place.
    assert zpf.reassembly.stream_layer([], {}) is zpf.OutputLayer.TRANSPORT


def test_an_unrecognised_output_layer_is_reported_not_guessed():
    decoders = {1: zpf.Decoder(decoder_id=1, output_layer=9)}
    assert zpf.reassembly.stream_layer([_rec(1)], decoders) == 9


def test_an_unrecognised_layer_has_no_offset_space():
    """Both spaces are wrong for a layer we cannot name, so neither is used."""
    participant = zpf.Participant(session_id=7, participant_id=0)
    with pytest.raises(zpf.SemanticError, match="not one this version defines"):
        zpf.reassembly.record_ranges(participant, [_rec(1)], 9)


def test_a_participant_mixing_layers_has_no_single_answer():
    """A reader MUST NOT pick one: every later offset would be in the wrong space."""
    decoders = {
        1: zpf.Decoder(decoder_id=1, output_layer=zpf.OutputLayer.DECODED),
        2: zpf.Decoder(decoder_id=2, output_layer=zpf.OutputLayer.TRANSPORT),
    }
    with pytest.raises(zpf.SemanticError, match="mixes layers"):
        zpf.reassembly.stream_layer([_rec(1), _rec(2)], decoders)


def test_a_decoder_less_record_beside_a_decoded_one_also_mixes():
    # The other half of the rule: absence resolves to transport, which is a
    # layer like any other, so this is the same violation.
    decoders = {1: zpf.Decoder(decoder_id=1, output_layer=zpf.OutputLayer.DECODED)}
    with pytest.raises(zpf.SemanticError, match="mixes layers"):
        zpf.reassembly.stream_layer([_rec(), _rec(1)], decoders)


def test_an_undeclared_decoder_leaves_the_layer_unresolvable():
    with pytest.raises(zpf.SemanticError, match="undeclared decoder_id 4"):
        zpf.reassembly.stream_layer([_rec(4)], {})


# --- Unplaceable records: one offset rule, three call sites (#63) ---------------------


def malformed(*records: zpf.Record, isn: int | None = 1000) -> zpf.FileReader:
    """Write a file the checked writer would refuse, and reopen it.

    Deliberately the **flat** writer. These streams are the ones a producer
    should not have written — `zpf.create` grows a guard against them in the
    plan's Phase 4 — but a reader still meets them in the wild, which is the
    whole of #63: zpfwire has written a below-origin SYN into every file it
    has ever produced.
    """
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as w:
        w.write(zpf.FileHeader(tick_hz=1_000_000))
        w.write(zpf.Source(source_id=0, kind=zpf.SourceKind.CAPTURE))
        w.write(zpf.Session(session_id=7, proto="tcp"))
        w.write(zpf.Participant(session_id=7, participant_id=0, isn=isn))
        for record in records:
            w.write(record)
        w.write(zpf.End())
    return zpf.open(io.BytesIO(sink.getvalue()))


def rec(seq_start: int | None, payload: bytes, ts: int, **kwargs: object) -> zpf.Record:
    return zpf.Record(
        session_id=7, sender_pid=0, source_id=0, timestamp=ts,
        payload=payload, seq_start=seq_start, **kwargs,
    )


def measure(reader: zpf.FileReader) -> tuple[tuple[tuple[int, int], ...], int]:
    """Return ``(ranges, extent)`` for the file's one participant stream."""
    session = reader.session(7)
    blocks = list(session.stream_blocks(0))
    layer = session.layer(0)
    return (
        zpf.record_ranges(session.participant(0), blocks, layer),
        zpf.stream_extent(session.participant(0), blocks, layer),
    )


def test_a_conformant_handshake_is_unmoved():
    """The regression guard: the fix must not touch a correct file.

    This is #63's second trace — the SYN written where `0.17` made it a MUST,
    at ``isn + 1``. It shares its ``seq_start`` with the first data record,
    which the ordering rule explicitly allows (`0.18`, non-descending), and
    the SYN's zero length means the two do not overlap.
    """
    with malformed(
        rec(1001, b"", 900, flags=zpf.RecordFlags.SYN),
        rec(1001, b"AAAA", 1000),
        rec(1005, b"BBBB", 2000),
        rec(1009, b"CCCC", 3000),
    ) as reader:
        ranges, extent = measure(reader)
    assert ranges == ((0, 0), (0, 4), (4, 8), (8, 12))
    assert extent == 12


def test_a_syn_one_below_the_origin_is_unplaceable():
    """#63's first trace: what zpfwire writes, and what it used to do to us.

    The SYN sits at ``isn`` rather than ``isn + 1`` — one below the origin,
    because the SYN consumes a sequence number without delivering a byte.
    Before the fix this record landed at 4 294 967 295 and dragged the extent
    with it, so every coverage answer about the stream was wrong by four
    billion. It is now unplaceable: zero width, no bytes covered, and the
    three real records place exactly as they do in a conformant file.
    """
    with malformed(
        rec(1000, b"", 900, flags=zpf.RecordFlags.SYN),
        rec(1001, b"AAAA", 1000),
        rec(1005, b"BBBB", 2000),
        rec(1009, b"CCCC", 3000),
    ) as reader:
        ranges, extent = measure(reader)
    assert ranges == ((0, 0), (0, 4), (4, 8), (8, 12))
    assert extent == 12


def test_a_below_origin_record_carrying_payload_loses_its_bytes():
    """The case the issue's own suggested fix would not have caught.

    #63 proposed skipping zero-length records when computing the offset, which
    makes its symptom disappear and leaves this shape wrapping to 2³² − 1. The
    eight bytes are excluded from the extent and from every coverage answer
    the file supports, which is the price of not trusting the wrapped offset —
    and `unplaceable-below-origin` is the vector that pins it.
    """
    with malformed(
        rec(1000, b"LOSTBYTE", 1000),
        rec(1001, b"AAAABBBB", 2000),
        rec(1009, b"CCCCDDDD", 3000),
    ) as reader:
        ranges, extent = measure(reader)
        view = reader.session(7).reassemble()[0]
        chunks = list(view.chunks())
        units = list(view.units())
    assert ranges == ((0, 0), (0, 8), (8, 16))
    assert extent == 16
    # chunks and units agree: the unplaceable record contributes no bytes and
    # opens no gap. A reader that trusted the wrapped offset would yield a
    # four-billion-byte Gap here.
    assert chunks == [zpf.Segment(data=b"AAAABBBBCCCCDDDD", off_start=0, off_end=16, ts=3000)]
    assert [(u.off_start, u.off_end) for u in units] == [(0, 8), (8, 16)]


def test_a_record_with_no_seq_start_on_an_anchored_stream_is_unplaceable():
    """The commoner shape, and `unplaceable-no-seq-start`'s lesson.

    The stream is sequence-anchored — its first record carries a hint — so a
    record without one cannot be placed. It is *not* appended at the end: the
    six bytes are in no offset at all, and the extent stays 6.
    """
    with malformed(
        rec(1001, b"hinted", 3000),
        rec(None, b"plain2", 3200),
        isn=None,
    ) as reader:
        ranges, extent = measure(reader)
    assert ranges == ((0, 6), (6, 6))
    assert extent == 6


def test_the_unplaceable_range_is_the_running_maximum_not_the_last_end():
    """Why the placement is a maximum, which is what `0.18` settled and `0.19` unpinned.

    Records within a participant may overlap, so the record stored last is not
    always the one that reached furthest. Here the second record is a
    retransmit ending at 4 while the first reached 8, and the unplaceable
    third sits at 8 rather than at 4.
    """
    with malformed(
        rec(1001, b"AAAABBBB", 1000),  # [0, 8)
        rec(1001, b"AAAA", 2000),      # [0, 4) — a retransmit, ends lower
        rec(None, b"zzz", 3000),
    ) as reader:
        ranges, extent = measure(reader)
    assert ranges == ((0, 8), (0, 4), (8, 8))
    assert extent == 8


def test_stream_extent_is_what_the_coverage_check_measures_with():
    """The delegation #63 exists to protect, stated as a test.

    `transform.check_coverage` measures an input stream through
    :func:`zpf.stream_extent` rather than computing its own offsets, so the
    reader and the coverage checker cannot drift apart. A below-origin record
    used to make that shared number 4 294 967 303, and every coverage answer
    derived from it was wrong in the same way.
    """
    with malformed(
        rec(1000, b"LOSTBYTE", 1000),
        rec(1001, b"AAAABBBB", 2000),
    ) as reader:
        _, extent = measure(reader)
    assert extent == 8
    assert extent < SEQ_SPACE  # the wrapped reading was 4294967303


# --- Per-unit timestamps: the rule is per span set, not per run (#62) -----------------


def test_a_run_carries_the_time_of_each_record_that_built_it():
    """#62's own case, and what `Segment.ts` cannot answer.

    Three messages arrive in three packets and reassembly offers them as one
    run. The run's `ts` is 3000 for all of it, which is right for the run and
    wrong for two of the three units — the specification stamps a decoded
    record with the time of the last source element **in its span set**.
    """
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("10.0.0.1:51000", isn=1000)
        s.record(p, ts=1000, payload=b"AAAA", seq_start=1001)
        s.record(p, ts=2000, payload=b"BBBB", seq_start=1005)
        s.record(p, ts=3000, payload=b"CCCC", seq_start=1009)

    (segment,) = only_view(fill).segments()
    assert segment.ts == 3000  # the run completed then
    assert [segment.ts_for(a, b) for a, b in ((0, 4), (4, 8), (8, 12))] == [1000, 2000, 3000]
    # A unit spanning the whole run is the one case where `ts` is the answer.
    assert segment.ts_for(0, 12) == segment.ts


def test_a_unit_straddling_two_records_completes_with_the_later():
    """The span set is what decides, so a unit built from two packets waits."""
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("10.0.0.1:51000", isn=1000)
        s.record(p, ts=1000, payload=b"AAAA", seq_start=1001)
        s.record(p, ts=2000, payload=b"BBBB", seq_start=1005)

    (segment,) = only_view(fill).segments()
    assert segment.ts_for(2, 6) == 2000
    assert segment.ts_first_for(2, 6) == 1000  # but it started arriving earlier


def test_a_retransmit_contributing_no_accepted_byte_does_not_move_the_time():
    """The case a hand-rolled table gets wrong, and why this is an API.

    Under the favour-old overlap policy the second record's bytes are already
    covered, so it contributes nothing and its later timestamp must not reach
    the unit. Building the table from record timestamps outside the library
    stamps this range 9999 — wrong, and wrong only on a lossy capture, which
    is the worst place to find out.
    """
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("10.0.0.1:51000", isn=1000)
        s.record(p, ts=1000, payload=b"AAAA", seq_start=1001)
        s.record(p, ts=9999, payload=b"AAAA", seq_start=1001,
                 flags=zpf.RecordFlags.RETRANSMIT)
        s.record(p, ts=2000, payload=b"BBBB", seq_start=1005)

    (segment,) = only_view(fill).segments()
    assert segment.data == b"AAAABBBB"
    # The retransmit is absent from the contributors entirely: trimming
    # happens first, so it never had accepted bytes to record.
    assert [(c.off_start, c.off_end, c.ts) for c in segment.contributors] == [
        (0, 4, 1000),
        (4, 8, 2000),
    ]
    assert segment.ts_for(0, 4) == 1000
    assert segment.ts == 2000


def test_ts_first_falls_back_to_the_records_own_timestamp():
    """A record declaring no ts_first states only one arrival time."""
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("10.0.0.1:51000", isn=1000)
        s.record(p, ts=1000, payload=b"AAAA", seq_start=1001, ts_first=900)
        s.record(p, ts=2000, payload=b"BBBB", seq_start=1005)

    (segment,) = only_view(fill).segments()
    assert segment.ts_first_for(0, 4) == 900
    assert segment.ts_first_for(4, 8) == 2000  # no ts_first: its timestamp stands


def test_a_hand_built_segment_falls_back_to_the_runs_time():
    """No contributors to read, so the run's own time is all there is."""
    segment = zpf.Segment(data=b"abcd", off_start=0, off_end=4, ts=42)
    assert segment.ts_for(0, 2) == 42
    assert segment.ts_first_for(0, 2) == 42


def test_the_stream_answers_the_same_question_for_absolute_offsets():
    """`StreamView.ts_for` is what a decode stage uses, `cites` being absolute."""
    def fill(s: zpf.SessionWriter) -> None:
        p = s.participant("10.0.0.1:51000", isn=1000)
        s.record(p, ts=1000, payload=b"AAAA", seq_start=1001)
        s.record(p, ts=2000, payload=b"BBBB", seq_start=1005)

    view = only_view(fill)
    assert view.ts_for(0, 4) == 1000
    assert view.ts_for(4, 8) == 2000
    assert view.ts_for(0, 8) == 2000
    assert view.ts_for(8, 12) is None  # nothing there to have a time

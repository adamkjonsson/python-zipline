"""Tests for derive_from() scaffolding and the decode_stage orchestrator."""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING

import pytest

import zpf

if TYPE_CHECKING:
    from pathlib import Path

REQUEST = b"GET /a HTTP/1.1\r\n\r\n"
RESPONSE = b"HTTP/1.1 200 OK\r\n\r\n"


def raw_file(*, gap: bool = False) -> bytes:
    """Build a two-sided TCP session, optionally with a hole in the client stream."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000, time_epoch=42) as writer:
        writer.add_source("capture", uri="both.pcap")
        with writer.begin_session(proto="tcp", key="c <-> s", session_id=7) as session:
            client = session.participant("10.0.0.1:51000", isn=1000)
            server = session.participant("93.184.216.34:80", isn=5000)
            session.record(client, ts=10, payload=REQUEST, seq_start=1001)
            if gap:
                # 4 bytes lost, then more request bytes.
                session.record(client, ts=11, payload=b"MORE", seq_start=1001 + len(REQUEST) + 4)
            session.record(
                server, ts=12, payload=RESPONSE, seq_start=5001, ack=1001 + len(REQUEST)
            )
    return sink.getvalue()


def raw_path(tmp_path: Path, **kwargs: bool) -> Path:
    path = tmp_path / "raw.zpf"
    path.write_bytes(raw_file(**kwargs))
    return path


# --- FileWriter.derive_from -----------------------------------------------------------


def test_derive_from_mirrors_sources_sessions_and_participants(tmp_path: Path):
    path = raw_path(tmp_path)
    sink = io.BytesIO()
    with (
        zpf.open(path) as reader,
        zpf.create(
            sink,
            tick_hz=1_000_000,
            time_epoch=42,
            produced_by="t 1.0",
            produced_at=1_700_000_000,
        ) as writer,
    ):
        derived = writer.derive_from(reader, proto="http")
        assert derived.source.kind is zpf.SourceKind.ZPF_INPUT
        assert set(derived.sessions) == {7}
        assert set(derived.participants) == {(7, 0), (7, 1)}
        # Ids are preserved, so spans need no translation.
        assert [h.pid for h in derived.participants.values()] == [0, 1]
        (client,) = [p for p in reader.session(7).participants if p.participant_id == 0]
        assert derived.handle(client).pid == 0

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert out.sources[0].uri == str(path)
        assert out.sources[0].digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        session = out.session(7)
        assert session.proto == "http"
        assert session.key == "c <-> s"
        assert [p.endpoint for p in session.participants] == [
            "10.0.0.1:51000",
            "93.184.216.34:80",
        ]
        assert [p.isn for p in session.participants] == [None, None]  # not the raw stream


def test_derive_from_rejects_a_mismatched_time_base(tmp_path: Path):
    path = raw_path(tmp_path)
    with (
        zpf.open(path) as reader,
        zpf.create(io.BytesIO(), tick_hz=1_000, produced_by="t", produced_at=1) as writer,
        pytest.raises(zpf.ZpfError, match="tick_hz"),
    ):
        writer.derive_from(reader)


def test_derive_from_rejects_a_mismatched_time_epoch(tmp_path: Path):
    path = raw_path(tmp_path)
    with (
        zpf.open(path) as reader,
        zpf.create(io.BytesIO(), tick_hz=1_000_000, produced_by="t", produced_at=1) as writer,
        pytest.raises(zpf.ZpfError, match="time_epoch"),
    ):
        writer.derive_from(reader)


def test_derive_from_accepts_an_explicit_uri_and_digest(tmp_path: Path):
    sink = io.BytesIO()
    with (
        zpf.open(io.BytesIO(raw_file())) as reader,
        zpf.create(
            sink,
            tick_hz=1_000_000,
            time_epoch=42,
            produced_by="t 1.0",
            produced_at=1_700_000_000,
        ) as writer,
    ):
        assert reader.path is None  # opened from a stream
        writer.derive_from(reader, uri="s3://bucket/raw.zpf", digest="sha256:beef")
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert out.sources[0].uri == "s3://bucket/raw.zpf"
        assert out.sources[0].digest == "sha256:beef"


def test_reader_digest_matches_hashlib(tmp_path: Path):
    path = raw_path(tmp_path)
    with zpf.open(path) as reader:
        expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        assert reader.digest() == expected
        assert reader.digest() == expected  # position restored, so it repeats
        assert reader.digest("sha1").startswith("sha1:")
        # Reading still works after hashing rewound the stream.
        assert [r.payload for r in reader.session(7).stream(1)] == [RESPONSE]


# --- decode_stage ---------------------------------------------------------------------


def split_lines(data: bytes) -> list[tuple[int, int]]:
    """Split on CRLFCRLF, returning (start, end) of each complete message."""
    spans: list[tuple[int, int]] = []
    start = 0
    while (idx := data.find(b"\r\n\r\n", start)) != -1:
        spans.append((start, idx + 4))
        start = idx + 4
    return spans


def run_http_decode(source: object, sink: io.BytesIO) -> None:
    """Decode every stream of ``source`` into ``sink`` as a decode stage."""
    with zpf.decode_stage(
        source,
        sink,
        decoder=("http/1.1", "test"),
        produced_by="http-decode 1.0",
        produced_at=1_700_000_000,
        proto="http",
    ) as dec:
        for stream in dec.streams():
            for seg in stream.segments():
                for start, end in split_lines(seg.data):
                    dec.record(
                        stream,
                        seg.data[start:end],
                        ts=seg.ts,
                        content_type="dec:http-message",
                        cites=(seg.off_start + start, seg.off_start + end),
                    )


def test_decode_stage_writes_a_conformant_decode_stage_file(tmp_path: Path):
    path = raw_path(tmp_path)
    sink = io.BytesIO()
    run_http_decode(path, sink)

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert out.file_kind == "decode-stage"
        assert out.complete
        assert out.diagnostics == []
        assert out.header.tick_hz == 1_000_000  # copied from the input
        assert out.header.time_epoch == 42
        assert out.header.produced_by == "http-decode 1.0"
        assert out.decoders[0].name == "http/1.1"
        assert out.decoders[0].version == "test"
        records = list(out.session(7).records())
        assert [r.payload for r in records] == [REQUEST, RESPONSE]
        assert [r.sender_pid for r in records] == [0, 1]
        assert all(r.content_type == "dec:http-message" for r in records)


def test_decode_stage_cites_the_input_with_the_right_ids(tmp_path: Path):
    path = raw_path(tmp_path)
    sink = io.BytesIO()
    run_http_decode(path, sink)

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        request, response = out.session(7).records()
        (span,) = request.spans
        assert span == zpf.Span(
            source_id=0, session_id=7, participant_id=0, off_start=0, off_end=len(REQUEST)
        )
        (span,) = response.spans
        assert span.participant_id == 1  # cites the server's stream, not the client's
        assert (span.off_start, span.off_end) == (0, len(RESPONSE))


def test_decode_stage_output_passes_the_coverage_check(tmp_path: Path):
    path = raw_path(tmp_path)
    sink = io.BytesIO()
    run_http_decode(path, sink)
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), path) == []


def test_decode_stage_accepts_an_open_reader_and_leaves_it_open(tmp_path: Path):
    path = raw_path(tmp_path)
    sink = io.BytesIO()
    with zpf.open(path) as reader:
        run_http_decode(reader, sink)
        # The stage did not close a reader it did not open.
        assert [r.payload for r in reader.session(7).stream(0)] == [REQUEST]


def test_decode_stage_closes_an_input_it_opened(tmp_path: Path):
    path = raw_path(tmp_path)
    sink = io.BytesIO()
    with zpf.decode_stage(
        path,
        sink,
        decoder="http/1.1",
        produced_by="t 1.0",
        produced_at=1,
    ) as dec:
        reader = dec.reader
    with pytest.raises(zpf.ZpfError, match="closed"):
        list(reader.session(7).stream(0))


def test_decode_stage_undecoded_marks_a_gap(tmp_path: Path):
    path = raw_path(tmp_path, gap=True)
    sink = io.BytesIO()
    with zpf.decode_stage(
        path,
        sink,
        decoder="http/1.1",
        produced_by="t 1.0",
        produced_at=1,
    ) as dec:
        for stream in dec.streams():
            for chunk in stream.chunks():
                if isinstance(chunk, zpf.Gap):
                    dec.undecoded(stream, chunk.off_start, chunk.off_end, reason="gap")
                else:
                    dec.record(
                        stream,
                        chunk.data,
                        ts=chunk.ts,
                        content_type="dec:raw",
                        cites=(chunk.off_start, chunk.off_end),
                    )

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        (undecoded,) = out.undecoded
        assert undecoded.reason == "gap"
        assert (undecoded.session_id, undecoded.participant_id) == (7, 0)
        assert (undecoded.off_start, undecoded.off_end) == (len(REQUEST), len(REQUEST) + 4)


def test_a_failing_decode_leaves_the_output_incomplete(tmp_path: Path):
    path = raw_path(tmp_path)
    sink = io.BytesIO()
    with (
        pytest.raises(RuntimeError, match="decoder blew up"),
        zpf.decode_stage(
            path, sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
        ) as dec,
    ):
        stream = dec.streams()[0]
        dec.record(stream, b"partial", ts=1, cites=(0, 7))
        msg = "decoder blew up"
        raise RuntimeError(msg)
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert not out.complete  # no End block: honestly unfinished


# --- coverage auto-fill ---------------------------------------------------------------

MORE = b"MORE"


def undecoded_by_stream(data: bytes) -> dict[tuple[int, int, int, int], zpf.Undecoded]:
    with zpf.open(io.BytesIO(data)) as out:
        return {
            (u.session_id, u.participant_id, u.off_start, u.off_end): u for u in out.undecoded
        }


def test_autofill_marks_uncited_data_as_skipped():
    raw = raw_file()
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    ) as dec:
        client = dec.streams()[0]
        dec.record(client, b"GET", ts=1, cites=(0, 4))  # only the first 4 bytes

    marks = undecoded_by_stream(sink.getvalue())
    # The rest of the client stream, and the whole (untouched) server stream:
    # data the decoder passed over, not a claim that it is undecodable.
    assert marks[7, 0, 4, len(REQUEST)].reason == "skipped"
    assert marks[7, 1, 0, len(RESPONSE)].reason == "skipped"
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


def test_autofill_marks_a_reassembly_gap_as_gap():
    raw = raw_file(gap=True)
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    ) as dec:
        for stream in dec.streams():
            for seg in stream.segments():  # cite the data, but never the hole
                dec.record(stream, seg.data, ts=seg.ts, cites=(seg.off_start, seg.off_end))

    marks = undecoded_by_stream(sink.getvalue())
    (gap_key,) = [k for k, u in marks.items() if u.reason == "gap"]
    assert gap_key == (7, 0, len(REQUEST), len(REQUEST) + 4)
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


def test_autofill_splits_an_uncovered_range_around_a_gap():
    raw = raw_file(gap=True)
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    ):
        pass  # decode nothing at all

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        client = sorted(
            (u for u in out.undecoded if u.participant_id == 0), key=lambda u: u.off_start
        )
    assert [(u.off_start, u.off_end, u.reason) for u in client] == [
        (0, len(REQUEST), "skipped"),
        (len(REQUEST), len(REQUEST) + 4, "gap"),
        (len(REQUEST) + 4, len(REQUEST) + 4 + len(MORE), "skipped"),
    ]
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


def test_autofill_preserves_an_explicit_marker_and_fills_the_rest():
    raw = raw_file()
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    ) as dec:
        client = dec.streams()[0]
        dec.record(client, b"x", ts=1, cites=(0, 5))
        dec.undecoded(client, 5, 9, reason="truncated", comment="mid-parse")

    marks = undecoded_by_stream(sink.getvalue())
    explicit = marks[7, 0, 5, 9]
    assert explicit.reason == "truncated"  # not overwritten by auto-fill
    assert explicit.comment == "mid-parse"
    assert marks[7, 0, 9, len(REQUEST)].reason == "skipped"  # auto-filled remainder
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


def test_explicit_undecodable_coexists_with_auto_skipped():
    """The undecodable reason is the decoder's own claim; auto-fill only says skipped."""
    raw = raw_file()
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    ) as dec:
        client = dec.streams()[0]
        dec.undecoded(client, 0, 5, reason="undecodable")  # tried and failed
        # bytes [5, len) of the client and all of the server are auto-filled

    marks = undecoded_by_stream(sink.getvalue())
    assert marks[7, 0, 0, 5].reason == "undecodable"  # explicit, preserved
    assert marks[7, 0, 5, len(REQUEST)].reason == "skipped"  # auto
    assert marks[7, 1, 0, len(RESPONSE)].reason == "skipped"  # auto
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


def test_autofill_off_leaves_coverage_incomplete():
    raw = raw_file()
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw),
        sink,
        decoder="http/1.1",
        produced_by="t 1.0",
        produced_at=1,
        fill_undecoded=False,
    ) as dec:
        client = dec.streams()[0]
        dec.record(client, b"GET", ts=1, cites=(0, 4))

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert list(out.undecoded) == []  # nothing added on our behalf
    findings = zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw))
    assert {f.category for f in findings} == {"coverage-gap"}


def test_autofill_raises_when_a_cite_and_a_mark_overlap():
    raw = raw_file()
    sink = io.BytesIO()
    # The raise happens on context exit (auto-fill), not in the body.
    with (
        pytest.raises(zpf.ZpfError, match="both decoded and explicitly marked"),
        zpf.decode_stage(
            io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
        ) as dec,
    ):
        client = dec.streams()[0]
        dec.record(client, b"x", ts=1, cites=(0, 10))
        dec.undecoded(client, 5, 15, reason="undecodable")
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert not out.complete  # auto-fill failed: no End block


# --- cites shorthand ------------------------------------------------------------------


def stage_for(sink: io.BytesIO) -> zpf.DecodeStage:
    return zpf.decode_stage(
        io.BytesIO(raw_file()), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    )


def test_cites_accepts_a_pair_a_span_and_a_sequence():
    sink = io.BytesIO()
    with stage_for(sink) as dec:
        stream = dec.streams()[0]
        dec.record(stream, b"a", ts=1, cites=(0, 4))
        dec.record(stream, b"b", ts=2, cites=stream.cite(4, 8))
        dec.record(stream, b"c", ts=3, cites=[(0, 4), stream.cite(8, 12)])
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        first, second, third = out.session(7).records()
        assert [(s.off_start, s.off_end) for s in first.spans] == [(0, 4)]
        assert [(s.off_start, s.off_end) for s in second.spans] == [(4, 8)]
        assert [(s.off_start, s.off_end) for s in third.spans] == [(0, 4), (8, 12)]


def test_a_decode_stage_record_must_cite_its_input():
    # spans are what identify a record as built by *this* stage, and what
    # the coverage guarantee is checked against. An uncited record claims to
    # have been re-emitted unchanged, which a decode stage cannot mean.
    sink = io.BytesIO()
    with pytest.raises(zpf.SemanticError, match="exactly one kind"), stage_for(sink) as dec:
        stream = dec.streams()[0]
        dec.record(stream, b"a", ts=1, cites=(0, 4))
        dec.record(stream, b"b", ts=2)  # no citation at all


def test_cites_rejects_a_malformed_entry():
    sink = io.BytesIO()
    with stage_for(sink) as dec:
        stream = dec.streams()[0]
        with pytest.raises(zpf.ZpfError, match="off_start"):
            dec.record(stream, b"a", ts=1, cites=[(0, 4, 9)])


def test_streams_are_paired_with_the_matching_output_participant():
    sink = io.BytesIO()
    with stage_for(sink) as dec:
        client, server = dec.streams()
        assert (client.session_id, client.pid) == (7, 0)
        assert (server.session_id, server.pid) == (7, 1)
        assert client.handle.pid == 0
        assert server.handle.pid == 1
        assert client.participant.endpoint == "10.0.0.1:51000"
        assert client.is_stream_oriented
        assert client.reassembled() == REQUEST
        assert dec.streams() is dec.streams()  # cached


# --- multiple decoders per session ----------------------------------------------------


def test_records_can_use_more_than_one_decoder_in_a_session():
    raw = raw_file()
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    ) as dec:
        http = dec.decoder
        other = dec.writer.add_decoder("json/1.0")  # a second decoder, declared up front
        client, server = dec.streams()
        dec.record(client, REQUEST, ts=1, cites=(0, len(REQUEST)))  # default -> http
        dec.record(server, RESPONSE, ts=2, cites=(0, len(RESPONSE)), decoder=other)

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        by_pid = {r.sender_pid: r.decoder_id for r in out.session(7).records()}
        assert by_pid == {0: http.decoder_id, 1: other.decoder_id}


def test_undecoded_can_be_attributed_to_a_non_default_decoder():
    raw = raw_file()
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw),
        sink,
        decoder="http/1.1",
        produced_by="t 1.0",
        produced_at=1,
        fill_undecoded=False,
    ) as dec:
        http = dec.decoder
        other = dec.writer.add_decoder("json/1.0")
        client, server = dec.streams()
        dec.undecoded(client, 0, len(REQUEST))  # default -> http
        dec.undecoded(server, 0, len(RESPONSE), decoder=other)  # override -> json

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        by_pid = {u.participant_id: u.decoder_id for u in out.undecoded}
        assert by_pid == {0: http.decoder_id, 1: other.decoder_id}


def test_autofill_always_uses_the_stage_default_decoder():
    raw = raw_file()
    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw), sink, decoder="http/1.1", produced_by="t 1.0", produced_at=1
    ) as dec:
        http = dec.decoder
        dec.writer.add_decoder("json/1.0")  # a second decoder exists, but decode nothing

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        # Both streams are auto-filled, all attributed to the stage's decoder.
        assert {u.decoder_id for u in out.undecoded} == {http.decoder_id}


def test_a_stage_declares_how_long_its_inputs_were(tmp_path: Path):
    """The autofill 0.14's SHOULD asks for.

    The stage knows every input stream's extent by the time it closes, and
    declaring it is what lets a consumer check coverage from the output alone
    — a trailing gap is otherwise indistinguishable from a short stream.
    """
    raw, out = tmp_path / "raw.zpf", tmp_path / "dec.zpf"
    with zpf.create(raw, tick_hz=1) as w:
        w.add_source("capture", uri="t.pcap")
        with w.begin_session(proto="tcp", session_id=7) as s:
            sender = s.participant("a", isn=1000)
            s.record(sender, ts=1, payload=b"HELLO WORLD", seq_start=1001)
    with zpf.decode_stage(raw, out, decoder="http/1.1", produced_by="d 1", produced_at=1) as stage:
        for stream in stage.streams():
            for datagram in stream.datagrams():
                stage.record(stream, ts=datagram.ts, payload=datagram.data[:5],
                             spans=(stream.cite(0, 5),))
    with zpf.open(out) as reader:
        end = reader.session(7).end
        assert end is not None
        assert [(e.participant_id, e.extent) for e in end.input_extents] == [(0, 11)]
        assert reader.diagnostics == []
    assert zpf.check_extents(out) == []
    assert zpf.check_coverage(out, raw) == []


def test_a_stage_can_break_its_own_output(tmp_path: Path):
    """The mirror of ``undecoded``: this one is about the output.

    It discharges no coverage obligation, so the tail the decoder skipped
    still has to be marked — which the auto-fill does.
    """
    raw, out = tmp_path / "raw.zpf", tmp_path / "dec.zpf"
    with zpf.create(raw, tick_hz=1) as w:
        w.add_source("capture", uri="t.pcap")
        with w.begin_session(proto="tcp", session_id=7) as s:
            sender = s.participant("a", isn=1000)
            s.record(sender, ts=1, payload=b"HELLO WORLD", seq_start=1001)
    with zpf.decode_stage(raw, out, decoder="tls", produced_by="d 1", produced_at=1) as stage:
        for stream in stage.streams():
            stage.record(stream, ts=0, payload=b"HELLO", spans=(stream.cite(0, 5),))
            stage.discontinuity(stream, reason="tls-record-lost")
    with zpf.open(out) as reader:
        (block,) = [b for b in reader.blocks() if isinstance(b, zpf.Discontinuity)]
        assert (block.session_id, block.participant_id) == (7, 0)
        assert block.width is None
        assert reader.diagnostics == []
        # Coverage is untouched by the break: the undecoded tail is still marked.
        marked = [b for b in reader.blocks() if isinstance(b, zpf.Undecoded)]
        assert [(b.off_start, b.off_end) for b in marked] == [(5, 11)]
    assert zpf.check_coverage(out, raw) == []

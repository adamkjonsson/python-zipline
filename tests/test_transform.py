"""Tests for the merge transform and the coverage validator."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

import zpf

KEY = "10.0.0.1:51000 <-> 93.184.216.34:80"


def write_side_a(sink: object) -> None:
    with zpf.create(sink, tick_hz=1_000_000) as w:
        w.add_source("capture", uri="sideA.pcap")
        s = w.begin_session(proto="tcp", key=KEY, session_id=7)
        client = s.participant("10.0.0.1:51000", isn=1000, tcp_role=zpf.TcpRole.INITIATOR)
        s.record(client, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n", seq_start=1001, ack=5001)


def write_side_b(sink: object) -> None:
    with zpf.create(sink, tick_hz=1_000_000) as w:
        w.add_source("capture", uri="sideB.pcap")
        s = w.begin_session(proto="tcp", key=KEY, session_id=3)
        server = s.participant("93.184.216.34:80", isn=5000, tcp_role=zpf.TcpRole.RESPONDER)
        # Skewed clock: stamped before the request it answers.
        s.record(server, ts=995, payload=b"HTTP/1.1 200 OK\r\n...", seq_start=5001, ack=1019)


@pytest.fixture
def sides(tmp_path: Path) -> tuple[Path, Path]:
    side_a, side_b = tmp_path / "sideA.zpf", tmp_path / "sideB.zpf"
    write_side_a(side_a)
    write_side_b(side_b)
    return side_a, side_b


def test_merge_produces_the_specs_pass_through_file(sides: tuple[Path, Path], tmp_path: Path):
    side_a, side_b = sides
    output = tmp_path / "merged.zpf"
    zpf.merge_files(side_a, side_b, output, produced_by="zpf-merge 1.2", produced_at=1_719_510_000)
    with zpf.open(output) as merged:
        assert merged.stream_kind(merged.sessions()[0].session_id, 0) == (
            zpf.SourceKind.ZPF_INPUT,
            zpf.OutputLayer.TRANSPORT,
        )
        assert merged.complete
        assert merged.diagnostics == []
        header = merged.header
        assert (header.produced_by, header.produced_at) == ("zpf-merge 1.2", 1_719_510_000)
        assert [s.uri for s in merged.sources.values()] == [str(side_a), str(side_b)]
        (session,) = merged.sessions()
        assert session.sequenced
        assert (session.proto, session.key) == ("tcp", KEY)
        client, server = session.participants
        assert client.origin == zpf.Origin(source_id=0, session_id=7, participant_id=0)
        assert server.origin == zpf.Origin(source_id=1, session_id=3, participant_id=0)
        assert (client.isn, server.isn) == (1000, 5000)
        records = list(session.records())
        # Causal order despite the timestamp inversion.
        assert [r.payload[:3] for r in records] == [b"GET", b"HTT"]
        assert [(r.seq_start, r.ack) for r in records] == [(1001, 5001), (5001, 1019)]
        assert all(r.spans == () for r in records)  # pass-through: no spans
        assert all(r.decoder_id is None for r in records)
        session.verify()  # the baked-in order really is a causal linearization
        assert session.end is not None


def test_merge_records_input_digests(sides: tuple[Path, Path], tmp_path: Path):
    side_a, side_b = sides
    output = tmp_path / "merged.zpf"
    zpf.merge_files(side_a, side_b, output, produced_by="t 1")
    with zpf.open(output) as merged:
        digests = [s.digest for s in merged.sources.values()]
    expected = [
        f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}" for path in (side_a, side_b)
    ]
    assert digests == expected


def test_merge_accepts_streams_but_omits_digests():
    side_a, side_b = io.BytesIO(), io.BytesIO()
    write_side_a(side_a)
    write_side_b(side_b)
    side_a.seek(0)
    side_b.seek(0)
    output = io.BytesIO()
    zpf.merge_files(side_a, side_b, output, produced_by="t 1")
    with zpf.open(io.BytesIO(output.getvalue())) as merged:
        assert all(s.digest is None and s.uri is None for s in merged.sources.values())
        assert merged.sessions()[0].record_count == 2


def test_merge_rejects_mismatched_clocks(sides: tuple[Path, Path], tmp_path: Path):
    side_a, _ = sides
    other = tmp_path / "other.zpf"
    with zpf.create(other, tick_hz=1_000) as w:  # different tick rate
        w.add_source("capture")
        s = w.begin_session(proto="tcp")
        s.participant("x")
    with pytest.raises(zpf.ZpfError, match="tick_hz differs"):
        zpf.merge_files(side_a, other, tmp_path / "out.zpf", produced_by="t 1")


def test_merge_rejects_non_canonical_shapes(sides: tuple[Path, Path], tmp_path: Path):
    side_a, _ = sides
    two_sessions = tmp_path / "two_sessions.zpf"
    with zpf.create(two_sessions, tick_hz=1_000_000) as w:
        w.add_source("capture")
        w.begin_session(proto="tcp")
        w.begin_session(proto="tcp")
    with pytest.raises(zpf.ZpfError, match="2 sessions"):
        zpf.merge_files(side_a, two_sessions, tmp_path / "out.zpf", produced_by="t 1")

    two_participants = tmp_path / "two_participants.zpf"
    with zpf.create(two_participants, tick_hz=1_000_000) as w:
        w.add_source("capture")
        s = w.begin_session(proto="tcp")
        s.participant("a")
        s.participant("b")
    with pytest.raises(zpf.ZpfError, match="2 participants"):
        zpf.merge_files(side_a, two_participants, tmp_path / "out.zpf", produced_by="t 1")


def test_merge_rejects_an_already_merged_input_for_its_shape(
    sides: tuple[Path, Path], tmp_path: Path
):
    """Rejected for holding two directions, not for being derived.

    The bar used to be ``file_kind == "raw"``, which caught this file for
    the wrong reason. Since 0.15 made provenance and layer independent,
    being derived says nothing about whether a file is mergeable: a merged
    file is a transport stream and its offsets are preserved. What still
    disqualifies it is that the merge takes **one captured direction per
    input**, and this holds both.
    """
    side_a, side_b = sides
    merged = tmp_path / "merged.zpf"
    zpf.merge_files(side_a, side_b, merged, produced_by="t 1")
    with pytest.raises(zpf.ZpfError, match="2 participants"):
        zpf.merge_files(merged, side_b, tmp_path / "out.zpf", produced_by="t 1")


def test_merge_rejects_a_decoded_input(tmp_path: Path):
    """The layer is the bar, and a decoded stream fails it.

    Its offsets are a concatenation of record payloads, which a re-emission
    into a merged file does not preserve — so there is nothing for the merge
    to be faithful to, whatever the file's provenance.
    """
    raw = tmp_path / "raw.zpf"
    write_raw(raw)
    decoded = tmp_path / "decoded.zpf"
    write_decoded(decoded, raw, [(0, 139)], [])
    with pytest.raises(zpf.ZpfError, match="decoded layer"):
        zpf.merge_files(decoded, raw, tmp_path / "out.zpf", produced_by="t 1")


# --- check_coverage ---------------------------------------------------------------


def write_raw(path: Path, *, hole: bool = False) -> None:
    """Write a raw file: p0 extent 18 (optionally with a hole), p1 extent 139."""
    with zpf.create(path, tick_hz=1_000_000) as w:
        w.add_source("capture", uri="tap.pcap")
        s = w.begin_session(proto="tcp", session_id=7)
        p0 = s.participant("10.0.0.1:51000", isn=1000)
        p1 = s.participant("93.184.216.34:80", isn=5000)
        if hole:
            # [0, 10) then a 39-byte gap, then [49, 59): extent 59.
            s.record(p0, ts=1, payload=b"x" * 10, seq_start=1001)
            s.record(p0, ts=2, payload=b"y" * 10, seq_start=1050)
        else:
            s.record(p0, ts=1, payload=b"x" * 18, seq_start=1001)
        s.record(p1, ts=3, payload=b"z" * 139, seq_start=5001)


def write_decoded(
    path: Path,
    raw: Path,
    p1_spans: list[tuple[int, int]],
    p1_undecoded: list[tuple[int, int]],
) -> None:
    """Write a decode-stage file covering p0 fully and p1 as directed."""
    digest = f"sha256:{hashlib.sha256(raw.read_bytes()).hexdigest()}"
    with zpf.create(path, tick_hz=1_000_000, produced_by="dec 1", produced_at=1) as w:
        src = w.add_source("zpf-input", uri=str(raw), digest=digest)
        http = w.add_decoder("http/1.1")
        s = w.begin_session(proto="http", session_id=7)
        client = s.participant("10.0.0.1:51000")
        s.participant("93.184.216.34:80")
        s.record(
            client, ts=1, payload=b"req", decoder=http,
            spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                            off_start=0, off_end=18),),
        )
        for start, end in p1_spans:
            s.record(
                client, ts=2, payload=b"resp", decoder=http,
                spans=(zpf.Span(source_id=0, session_id=7, participant_id=1,
                                off_start=start, off_end=end),),
            )
        for start, end in p1_undecoded:
            w.undecoded(src, 7, 1, start, end, reason="undecodable", decoder=http)


def test_full_coverage_has_no_findings(tmp_path: Path):
    raw, decoded = tmp_path / "raw.zpf", tmp_path / "decoded.zpf"
    write_raw(raw)
    write_decoded(decoded, raw, p1_spans=[(0, 100)], p1_undecoded=[(100, 139)])
    assert zpf.check_coverage(decoded, raw) == []


def test_gap_overlap_and_excess_are_reported(tmp_path: Path):
    raw = tmp_path / "raw.zpf"
    write_raw(raw)

    gappy = tmp_path / "gappy.zpf"
    write_decoded(gappy, raw, p1_spans=[(0, 100)], p1_undecoded=[(110, 139)])
    (finding,) = zpf.check_coverage(gappy, raw)
    assert finding.category == "coverage-gap"
    assert "[100, 110)" in finding.message

    overlapping = tmp_path / "overlapping.zpf"
    write_decoded(overlapping, raw, p1_spans=[(0, 100)], p1_undecoded=[(90, 139)])
    (finding,) = zpf.check_coverage(overlapping, raw)
    assert finding.category == "coverage-overlap"
    assert "[90, 100)" in finding.message

    excessive = tmp_path / "excessive.zpf"
    write_decoded(excessive, raw, p1_spans=[(0, 150)], p1_undecoded=[])
    findings = zpf.check_coverage(excessive, raw)
    assert [f.category for f in findings] == ["coverage-excess"]
    assert "extent (139)" in findings[0].message


def test_hole_inclusive_extents(tmp_path: Path):
    raw = tmp_path / "raw.zpf"
    write_raw(raw, hole=True)  # p0: [0,10) + hole [10,49) + [49,59)
    decoded = tmp_path / "decoded.zpf"
    digest = f"sha256:{hashlib.sha256(raw.read_bytes()).hexdigest()}"
    with zpf.create(decoded, tick_hz=1_000_000, produced_by="dec 1", produced_at=1) as w:
        source = w.add_source("zpf-input", uri=str(raw), digest=digest)
        http = w.add_decoder("http/1.1")
        s = w.begin_session(proto="http", session_id=7)
        client = s.participant("10.0.0.1:51000")
        s.record(
            client, ts=1, payload=b"a", decoder=http,
            spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                            off_start=0, off_end=10),),
        )
        w.undecoded(source, 7, 0, 10, 49, reason="gap")
        s.record(
            client, ts=2, payload=b"b", decoder=http,
            spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                            off_start=49, off_end=59),),
        )
        w.undecoded(source, 7, 1, 0, 139, reason="undecodable")
    assert zpf.check_coverage(decoded, raw) == []


# --- Two-hop provenance resolution -------------------------------------------

CHAIN = Path(__file__).parent / "vectors/chain"


def test_a_decode_stages_record_resolves_in_one_hop():
    # It carries spans of its own: this file's stage built it.
    (span,) = zpf.resolve_spans(CHAIN / "decoded.zpf", 7, 0, 0)
    assert (span.off_start, span.off_end) == (0, 9)


def test_a_pass_throughs_record_resolves_in_two_hops():
    # annotated.zpf re-emits decoded.zpf's records unchanged, so they carry
    # no spans. The walk takes the participant's origin into decoded.zpf,
    # finds the record occupying the same offsets, and reads its spans —
    # which name raw.zpf. This file alone cannot answer the question.
    with zpf.open(CHAIN / "annotated.zpf") as f:
        assert [r.spans for r in f.session(7).records()] == [(), ()]
    (span,) = zpf.resolve_spans(CHAIN / "annotated.zpf", 7, 0, 0)
    assert (span.session_id, span.participant_id) == (7, 0)
    assert (span.off_start, span.off_end) == (0, 9)
    (other,) = zpf.resolve_spans(CHAIN / "annotated.zpf", 7, 1, 0)
    assert (other.off_start, other.off_end) == (0, 16)


def test_a_raw_file_records_no_provenance_to_resolve():
    assert zpf.resolve_spans(CHAIN / "raw.zpf", 7, 0, 0) == ()


def test_resolving_a_stream_needs_an_explicit_opener():
    with (
        (CHAIN / "annotated.zpf").open("rb") as handle,
        pytest.raises(zpf.ZpfError, match="open_input"),
    ):
        zpf.resolve_spans(handle, 7, 0, 0)


def test_an_opener_may_redirect_where_inputs_are_found():
    seen: list[str] = []

    def opener(source: zpf.Source) -> Path:
        seen.append(source.uri)
        return CHAIN / source.uri

    (span,) = zpf.resolve_spans(CHAIN / "annotated.zpf", 7, 0, 0, open_input=opener)
    assert seen == ["decoded.zpf"]  # only the immediate input needs opening
    assert (span.off_start, span.off_end) == (0, 9)


# --- Filter / reorder stages (rewrite_decoded) -------------------------------


def decoded_input(path: Path, payloads: tuple[bytes, ...] = (b"AAA", b"BB", b"CCCC")) -> None:
    """Write a decode-stage file with one participant carrying ``payloads``."""
    with zpf.create(path, tick_hz=1, produced_by="t", produced_at=1) as w:
        source = w.add_source("zpf-input", uri="raw.zpf")
        decoder = w.add_decoder("http/1.1", version="0.4")
        with w.begin_session(session_id=7) as session:
            sender = session.participant("client")
            offset = 0
            for index, body in enumerate(payloads):
                session.record(
                    sender, ts=index, payload=body, source=source, decoder=decoder,
                    content_type="dec:request",
                    spans=(zpf.Span(source_id=source.source_id, session_id=7,
                                    participant_id=0, off_start=offset,
                                    off_end=offset + len(body)),),
                )
                offset += len(body)


def test_a_filter_stage_is_a_decode_stage_not_a_pass_through(tmp_path: Path):
    # Dropping a record shifts every later offset in that participant's
    # space, so the output cannot claim to have preserved it.
    src, out = tmp_path / "decoded.zpf", tmp_path / "filtered.zpf"
    decoded_input(src)
    zpf.rewrite_decoded(src, out, keep=lambda r: r.payload != b"BB",
                        produced_by="zpf-filter 1.0", produced_at=2)
    with zpf.open(out) as f:
        assert f.stream_kind(7, 0) == (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.DECODED)
        assert f.diagnostics == []
        session = f.session(7)
        assert [r.payload for r in session.stream(0)] == [b"AAA", b"CCCC"]
        # Every surviving record cites the input range it came from.
        assert [[(s.off_start, s.off_end) for s in r.spans] for r in session.stream(0)] == [
            [(0, 3)], [(5, 9)]
        ]
        # The dropped range is marked, not silently lost.
        (marker,) = f.undecoded
        assert (marker.off_start, marker.off_end, marker.reason) == (3, 5, "skipped")


def test_the_coverage_guarantee_holds_over_a_filtered_stream(tmp_path: Path):
    src, out = tmp_path / "decoded.zpf", tmp_path / "filtered.zpf"
    decoded_input(src)
    zpf.rewrite_decoded(src, out, keep=lambda r: False,  # drop everything
                        produced_by="zpf-filter 1.0", produced_at=2)
    assert zpf.check_coverage(out, src) == []


def test_a_reordering_stages_spans_need_not_ascend(tmp_path: Path):
    # Stored order defines the output's offsets, so reordering creates a new
    # space; the cited input ranges then run backwards, which is expected.
    src, out = tmp_path / "decoded.zpf", tmp_path / "reversed.zpf"
    decoded_input(src)
    zpf.rewrite_decoded(src, out, reorder=lambda rs: list(reversed(rs)),
                        produced_by="zpf-reorder 1.0", produced_at=2)
    with zpf.open(out) as f:
        session = f.session(7)
        assert list(session.ranges(0)) == [(0, 4), (4, 6), (6, 9)]  # recomputed
        cited = [r.spans[0].off_start for r in session.stream(0)]
        assert cited == [5, 3, 0]  # descending: not stored order
    assert zpf.check_coverage(out, src) == []


def test_a_rewrite_inherits_decoders_rather_than_declaring_its_own(tmp_path: Path):
    # decoder_id names a layer, not a stage: a filtered HTTP message is
    # still an HTTP message, so the descriptors are re-declared as they were.
    src, out = tmp_path / "decoded.zpf", tmp_path / "filtered.zpf"
    decoded_input(src)
    zpf.rewrite_decoded(src, out, produced_by="zpf-filter 1.0", produced_at=2)
    with zpf.open(src) as original, zpf.open(out) as f:
        assert [d.name for d in f.decoders.values()] == [
            d.name for d in original.decoders.values()
        ]
        assert [d.version for d in f.decoders.values()] == ["0.4"]
        for record in f.session(7).stream(0):
            assert record.decoder_id is not None
            assert record.content_type == "dec:request"


def test_a_rewrite_needs_an_input_with_a_decoded_layer(tmp_path: Path):
    raw = tmp_path / "raw.zpf"
    write_raw(raw)
    with pytest.raises(zpf.ZpfError, match="decoded layer"):
        zpf.rewrite_decoded(raw, tmp_path / "out.zpf",
                            produced_by="zpf-filter 1.0", produced_at=2)


# --- check_extents: the coverage claims a single file can be checked for ------

VECTORS = Path(__file__).parent / "vectors"


def test_check_extents_needs_no_input_file():
    """The point of the function: coverage checkable from the file alone.

    ``check_coverage`` has to open the input to measure it, which a consumer
    handed one file cannot do. ``input_extents`` is what makes the weaker
    check possible.
    """
    path = VECTORS / "isolate-extent-exceeds-coverage/isolate-extent-exceeds-coverage.zpf"
    (finding,) = zpf.check_extents(path)
    assert finding.category == "extent-exceeds-coverage"
    assert "40" in finding.message


def test_check_extents_reports_one_finding_per_fault():
    disagree = VECTORS / "isolate-extents-disagree/isolate-extents-disagree.zpf"
    (finding,) = zpf.check_extents(disagree)
    # Session 101 declares 160 while the union of both sessions' spans reaches
    # 200, so a naive check would also report extent-below-coverage. There is
    # one fault here — the two declarations cannot both be right — and until
    # it is resolved there is no agreed extent to measure coverage against.
    assert finding.category == "extents-disagree"


def test_check_extents_finds_an_interior_hole_with_nothing_declared():
    path = VECTORS / "isolate-coverage-gap/isolate-coverage-gap.zpf"
    (finding,) = zpf.check_extents(path)
    assert finding.category == "coverage-gap"
    assert finding.offset == 10


def test_check_extents_passes_every_conformant_vector():
    """No false positive on anything upstream ships as ``accept``.

    ``session-fan-out`` is the one that matters: its ``[0, 80)`` is spanned by
    *both* output sessions, and neither session covers the extent 200 it
    declares — only the union across both does. A checker keyed on the output
    session fails it and passes every other file in the suite.
    """
    for name in ("decoded-basic", "broken-chain", "session-fan-out",
                 "annotator-decoded", "passthrough-discontinuity",
                 "discontinuity-known-width", "discontinuity-unknown-width"):
        path = VECTORS / name / f"{name}.zpf"
        assert zpf.check_extents(path) == [], name


def test_check_coverage_cross_checks_a_declared_extent_against_the_input(tmp_path: Path):
    """The check only the input file can settle.

    A declaration can agree with every span in its own file and still be
    wrong about the stream it measures. ``check_extents`` cannot see that;
    with the input in hand there is a real length to compare against.

    Built with the flat writer because the ergonomic ``SessionWriter.end``
    does not take ``input_extents`` yet — that is Phase 5's write side.
    """
    raw = tmp_path / "raw.zpf"
    write_raw(raw)
    with zpf.open(raw) as reader:
        session = next(iter(reader.sessions()))
        participant = session.participants[0]
        pid = participant.participant_id
        actual = zpf.reassembly.stream_extent(
            participant, list(session.stream_blocks(pid)), session.layer(pid)
        )

    decoded = tmp_path / "decoded.zpf"
    blocks = [
        zpf.FileHeader(tick_hz=1_000_000, produced_by="dec 1", produced_at=1),
        zpf.Source(source_id=0, kind=zpf.SourceKind.ZPF_INPUT, uri=str(raw)),
        zpf.Decoder(decoder_id=0, name="http/1.1"),
        zpf.Session(session_id=7, proto="http"),
        zpf.Participant(session_id=7, participant_id=0),
        zpf.Record(
            session_id=7, sender_pid=0, source_id=0, timestamp=1, payload=b"X", decoder_id=0,
            spans=(zpf.Span(source_id=0, session_id=7, participant_id=pid,
                            off_start=0, off_end=actual),),
        ),
        zpf.SessionEnd(
            session_id=7,
            input_extents=(
                zpf.InputExtent(source_id=0, session_id=7, participant_id=pid,
                                extent=actual + 5),
            ),
        ),
    ]
    with decoded.open("wb") as handle, zpf.BlockWriter(handle) as writer:
        for block in blocks:
            writer.write(block)

    # The two checks see different things. From the file alone, the
    # declaration overshoots what this file's own spans account for.
    assert [f.category for f in zpf.check_extents(decoded)] == ["extent-exceeds-coverage"]
    # With the input open, there is a real length to name. (The raw file's
    # second participant stream is undecoded here and reported too; this
    # test is about the extent, not that gap.)
    findings = zpf.check_coverage(decoded, raw)
    mismatch = [f for f in findings if f.category == "extent-mismatch"]
    assert len(mismatch) == 1
    assert str(actual) in mismatch[0].message


# --- check_splice: a violation belonging to a pair of files -------------------


def test_check_splice_catches_a_unit_welding_two_sides_of_a_break():
    stage1 = VECTORS / "splice/tls-records.zpf"
    stage2 = VECTORS / "splice/http.zpf"
    (finding,) = zpf.check_splice(stage2, stage1)
    assert finding.category == "discontinuity-splice"
    assert finding.offset == 50  # where the break sits in stage 1's output


def test_check_splice_is_quiet_when_the_stage_carries_the_break_forward(tmp_path: Path):
    """Two units with a break between them cross nothing.

    The escape the specification allows: emit your own Discontinuity in the
    corresponding position instead of spanning the join.
    """
    stage1 = tmp_path / "stage1.zpf"
    with zpf.create(stage1, tick_hz=1, produced_by="s1", produced_at=1) as w:
        source = w.add_source("zpf-input", uri="raw.zpf")
        decoder = w.add_decoder("tls")
        with w.begin_session(session_id=7) as s:
            client = s.participant("a")
            s.record(client, ts=0, payload=b"A" * 50, source=source, decoder=decoder,
                     spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                     off_start=0, off_end=50),))
            s.discontinuity(client, reason="tls-record-lost")
            s.record(client, ts=1, payload=b"B" * 30, source=source, decoder=decoder,
                     spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                     off_start=50, off_end=80),))

    def write_stage2(path: Path, *, weld: bool) -> None:
        with zpf.create(path, tick_hz=1, produced_by="s2", produced_at=2) as w:
            source = w.add_source("zpf-input", uri=str(stage1))
            decoder = w.add_decoder("http/1.1")
            with w.begin_session(session_id=7) as s:
                client = s.participant("a")
                if weld:
                    s.record(client, ts=0, payload=b"M", source=source, decoder=decoder,
                             spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                             off_start=0, off_end=80),))
                else:
                    s.record(client, ts=0, payload=b"M", source=source, decoder=decoder,
                             spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                             off_start=0, off_end=50),))
                    s.discontinuity(client, reason="tls-record-lost")
                    s.record(client, ts=1, payload=b"N", source=source, decoder=decoder,
                             spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                             off_start=50, off_end=80),))

    honest, welded = tmp_path / "honest.zpf", tmp_path / "welded.zpf"
    write_stage2(honest, weld=False)
    write_stage2(welded, weld=True)
    assert zpf.check_splice(honest, stage1) == []
    assert [f.category for f in zpf.check_splice(welded, stage1)] == ["discontinuity-splice"]


def test_a_units_spans_are_judged_together(tmp_path: Path):
    """Two spans either side of a break still weld the join into one unit.

    Neither span contains the break on its own, so a per-span check misses
    it. The rule is written about the *unit*.
    """
    stage1 = tmp_path / "stage1.zpf"
    with zpf.create(stage1, tick_hz=1, produced_by="s1", produced_at=1) as w:
        source = w.add_source("zpf-input", uri="raw.zpf")
        decoder = w.add_decoder("tls")
        with w.begin_session(session_id=7) as s:
            client = s.participant("a")
            s.record(client, ts=0, payload=b"A" * 50, source=source, decoder=decoder,
                     spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                     off_start=0, off_end=50),))
            s.discontinuity(client, reason="stream-gap")
            s.record(client, ts=1, payload=b"B" * 30, source=source, decoder=decoder,
                     spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                     off_start=50, off_end=80),))
    stage2 = tmp_path / "stage2.zpf"
    with zpf.create(stage2, tick_hz=1, produced_by="s2", produced_at=2) as w:
        source = w.add_source("zpf-input", uri=str(stage1))
        decoder = w.add_decoder("http/1.1")
        with w.begin_session(session_id=7) as s:
            client = s.participant("a")
            s.record(
                client, ts=0, payload=b"M", source=source, decoder=decoder,
                spans=(
                    zpf.Span(source_id=0, session_id=7, participant_id=0,
                             off_start=0, off_end=50),
                    zpf.Span(source_id=0, session_id=7, participant_id=0,
                             off_start=50, off_end=80),
                ),
            )
    assert [f.category for f in zpf.check_splice(stage2, stage1)] == ["discontinuity-splice"]


# --- rewrite_decoded against an input that breaks -----------------------------


def decoded_with_a_break(path: Path) -> None:
    """Write a decode stage whose output carries a declared break."""
    with zpf.create(path, tick_hz=1, produced_by="t", produced_at=1) as w:
        source = w.add_source("zpf-input", uri="raw.zpf")
        decoder = w.add_decoder("tls")
        with w.begin_session(session_id=7) as s:
            client = s.participant("a")
            for index, (body, (start, end)) in enumerate(
                [(b"AAA", (0, 10)), (b"BB", (10, 20)), (b"CCCC", (30, 40))]
            ):
                if index == 2:
                    s.discontinuity(client, width=5, reason="tls-record-lost")
                s.record(client, ts=index, payload=body, source=source, decoder=decoder,
                         spans=(zpf.Span(source_id=0, session_id=7, participant_id=0,
                                         off_start=start, off_end=end),))
            w.undecoded(source, 7, 0, 20, 30, reason="undecodable")


def test_a_rewrite_carries_its_inputs_breaks_forward(tmp_path: Path):
    """Duty 1 of the MUST NOT, and the one the standard actually requires.

    A filter re-emits records rather than merging them, so it satisfies the
    rule by putting every input break back between the same two units. Drop
    them and the output welds records the input said do not join.
    """
    src, out = tmp_path / "in.zpf", tmp_path / "out.zpf"
    decoded_with_a_break(src)
    # Nothing is dropped or reordered here, so the only break in the output
    # is the input's own — this stage adds none of its own.
    zpf.rewrite_decoded(src, out, produced_by="f 1", produced_at=2)
    with zpf.open(out) as reader:
        breaks = [b for b in reader.blocks() if isinstance(b, zpf.Discontinuity)]
    assert [(b.width, b.reason) for b in breaks] == [(5, "tls-record-lost")]
    assert zpf.check_splice(out, src) == []


def test_a_rewrite_marks_a_break_as_a_hole_not_as_skipped(tmp_path: Path):
    """A break's range holds no bytes, and the reason has to say so.

    ``skipped`` is the ``bytes`` class — the data exists upstream, go and
    fetch it. For the range a declared width covers that is false, and acting
    on it would send a consumer after bytes that were never sent.
    """
    src, out = tmp_path / "in.zpf", tmp_path / "out.zpf"
    decoded_with_a_break(src)
    zpf.rewrite_decoded(src, out, keep=lambda r: r.payload != b"BB",
                        produced_by="f 1", produced_at=2)
    with zpf.open(out) as reader:
        marked = [b for b in reader.blocks() if isinstance(b, zpf.Undecoded)]
        assert reader.diagnostics == []
    by_range = {(b.off_start, b.off_end): b.reason for b in marked}
    assert by_range[(5, 10)] == "gap"  # the break: no bytes anywhere
    assert zpf.UNDECODED_REASONS["gap"] == "hole"
    assert by_range[(3, 5)] == "skipped"  # the dropped record: bytes upstream
    assert zpf.check_coverage(out, src) == []


def test_a_drop_declares_the_width_it_withheld(tmp_path: Path):
    """The duty 0.15 made normative, and the width that tells it apart.

    Dropping the middle record leaves its neighbours adjacent in the output
    when they did not adjoin in the input — the defect a Discontinuity
    exists to prevent, one hop along. Through 0.14 no rule covered it and
    this library emitted the block behind a switch; 0.15 made it a MUST, so
    the switch is gone.

    A drop withheld content of *known* extent, so the width is declared —
    which is what ``filtered-decoded`` ships.
    """
    src, out = tmp_path / "in.zpf", tmp_path / "out.zpf"
    decoded_with_a_break(src)
    zpf.rewrite_decoded(src, out, keep=lambda r: r.payload != b"BB",
                        produced_by="f 1", produced_at=2)
    with zpf.open(out) as reader:
        breaks = [b for b in reader.blocks() if isinstance(b, zpf.Discontinuity)]
    assert [(b.width, b.reason) for b in breaks] == [
        (5, "tls-record-lost"),  # the input's own, carried forward
        (2, "records-dropped"),  # this stage's, and it knows the extent
    ]


def test_a_reorder_declares_no_width(tmp_path: Path):
    """The mirror case, and the specification is explicit about it.

    A reordering stage withholds nothing — every byte reaches the output —
    but two records stored as neighbours assert that they join, and for
    reordered neighbours that is false. What lies between two units that
    were never adjacent is not a hole to be counted, so there is no width to
    declare and the block contributes 0.
    """
    src, out = tmp_path / "in.zpf", tmp_path / "out.zpf"
    decoded_with_a_break(src)
    zpf.rewrite_decoded(src, out, reorder=lambda rs: list(reversed(rs)),
                        produced_by="f 1", produced_at=2)
    with zpf.open(out) as reader:
        breaks = [b for b in reader.blocks() if isinstance(b, zpf.Discontinuity)]
    assert all(b.width is None for b in breaks if b.reason == "reordered")
    assert "reordered" in [b.reason for b in breaks]


def test_a_rewrite_can_record_its_own_configuration(tmp_path: Path):
    """The gap 0.14 closed: a transform that decodes nothing can now say how."""
    src, out = tmp_path / "in.zpf", tmp_path / "out.zpf"
    decoded_with_a_break(src)
    zpf.rewrite_decoded(src, out, produced_by="f 1", produced_at=2,
                        transform_params_digest="sha256:c0ffee")
    with zpf.open(out) as reader:
        assert reader.header.transform_params_digest == "sha256:c0ffee"


def test_check_coverage_refuses_an_open_reader(tmp_path: Path):
    """#57: this was an AttributeError from inside reader.py's stream plumbing.

    Validating a stage's output against its input is exactly the moment both
    files are already open, so the wrong call is the natural one to make.
    """
    raw = tmp_path / "raw.zpf"
    write_raw(raw)
    with (
        zpf.open(raw) as reader,
        pytest.raises(TypeError, match="FileReader"),
    ):
        zpf.check_coverage(reader, raw)

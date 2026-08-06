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
        assert merged.file_kind == "pass-through"
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


def test_merge_rejects_derived_inputs(sides: tuple[Path, Path], tmp_path: Path):
    side_a, side_b = sides
    merged = tmp_path / "merged.zpf"
    zpf.merge_files(side_a, side_b, merged, produced_by="t 1")
    with pytest.raises(zpf.ZpfError, match="pass-through file"):
        zpf.merge_files(merged, side_b, tmp_path / "out.zpf", produced_by="t 1")


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
        assert f.file_kind == "decode-stage"
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

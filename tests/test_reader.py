"""Tests for the session-first reader (zpf.open)."""

from __future__ import annotations

import dataclasses
import io
import json
import pathlib
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest
from test_golden import GOLDEN, GOLDEN_BLOCKS
from test_jsonl import CHAT_EXAMPLE, DECODED_EXAMPLE, MERGED_EXAMPLE

import zpf
from zpf import _frame

if TYPE_CHECKING:
    from pathlib import Path


def open_bytes(data: bytes, **kwargs: object) -> zpf.FileReader:
    return zpf.open(io.BytesIO(data), **kwargs)


def open_text(text: str, **kwargs: object) -> zpf.FileReader:
    return zpf.open(io.StringIO(text), **kwargs)


# --- Golden file and spec examples ------------------------------------------------


def test_open_the_golden_file():
    with open_bytes(GOLDEN) as f:
        assert f.face == "binary"
        assert f.stream_kind(7, 0) == (zpf.SourceKind.CAPTURE, zpf.OutputLayer.TRANSPORT)
        assert not f.complete  # the worked example has no End block
        assert not f.truncated
        assert f.diagnostics == []
        assert f.header == zpf.FileHeader(tick_hz=1_000_000)
        assert f.sources[1].uri == "sideA.pcap"
        (session,) = f.sessions()
        assert (session.session_id, session.proto) == (7, "tcp")
        assert not session.sequenced
        assert session.end is None
        (participant,) = session.participants
        assert participant.endpoint == "10.0.0.1:51000"
        assert session.participant(0) is participant
        assert session.record_count == 1
        (record,) = session.records()
        assert record == GOLDEN_BLOCKS[-1]


def test_open_the_golden_file_from_a_path(tmp_path: Path):
    path = tmp_path / "golden.zpf"
    path.write_bytes(GOLDEN)
    with zpf.open(path) as f:
        (session,) = f.sessions()
        assert [r.payload for r in session.records()] == [b"GET / HTTP/1.1\r\n\r\n"]


def test_open_the_merged_example_jsonl():
    with open_text(MERGED_EXAMPLE) as f:
        assert f.face == "jsonl"
        assert f.stream_kind(1, 0) == (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.TRANSPORT)
        session = f.session(1)
        assert session.sequenced
        assert session.key == "10.0.0.1:51000 <-> 93.184.216.34:80"
        assert [p.origin.session_id for p in session.participants] == [7, 3]
        # A sequenced session's timeline is its stored order.
        assert list(session.timeline()) == list(session.records())
        assert [r.sender_pid for r in session.timeline()] == [0, 1]


def test_open_the_decoded_example_jsonl():
    with open_text(DECODED_EXAMPLE) as f:
        assert f.stream_kind(7, 0) == (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.DECODED)
        assert f.decoders[1].name == "http/1.1"
        (undecoded,) = f.undecoded
        assert (undecoded.off_start, undecoded.off_end) == (100, 139)
        assert undecoded.reason == "undecodable"
        session = f.session(7)
        records = list(session.records())
        assert [r.content_type for r in records] == ["dec:request", "dec:response"]


def test_open_the_chat_example_jsonl():
    with open_text(CHAT_EXAMPLE) as f:
        session = f.session(8)
        assert [p.endpoint for p in session.participants] == ["alice", "bob", "carol", "dave"]
        assert session.end is not None
        assert session.end.reason == "timeout"
        assert [r.payload for r in session.stream(3)] == [b"am I late?"]


# --- Laziness and interleaving (binary face) ----------------------------------------


def two_session_file() -> bytes:
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1) as w:
        w.add_source("capture")
        tcp = w.begin_session(proto="tcp")
        udp = w.begin_session(proto="udp")
        a = tcp.participant("a")
        feed = udp.participant("feed")
        # Interleave the two sessions' records in the file.
        tcp.record(a, ts=1, payload=b"t1")
        udp.record(feed, ts=2, payload=b"u1")
        tcp.record(a, ts=3, payload=b"t2")
        udp.record(feed, ts=4, payload=b"u2")
        tcp.record(a, ts=5, payload=b"t3")
    return sink.getvalue()


def test_interleaved_sessions_filter_correctly():
    with open_bytes(two_session_file()) as f:
        tcp, udp = f.sessions()
        assert [r.payload for r in tcp.records()] == [b"t1", b"t2", b"t3"]
        assert [r.payload for r in udp.records()] == [b"u1", b"u2"]


def test_interleaved_iterators_are_independent():
    with open_bytes(two_session_file()) as f:
        tcp, udp = f.sessions()
        tcp_iter = tcp.records()
        udp_iter = udp.records()
        assert next(tcp_iter).payload == b"t1"
        assert next(udp_iter).payload == b"u1"
        assert next(tcp_iter).payload == b"t2"
        assert next(udp_iter).payload == b"u2"
        assert next(tcp_iter).payload == b"t3"


def skewed_two_sided_file() -> bytes:
    """One unsequenced TCP session, response timestamped before the request."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000) as w:
        w.add_source("capture", uri="both.pcap")
        with w.begin_session(proto="tcp", session_id=7) as s:
            client = s.participant("10.0.0.1:51000", isn=1000)
            server = s.participant("93.184.216.34:80", isn=5000)
            # Server's clock is skewed: its response says ts 995, before the
            # request (ts 1000) it answers. Its ack proves the true order.
            s.record(server, ts=995, payload=b"HTTP/1.1 200 OK\r\n...",
                     seq_start=5001, ack=1019)
            s.record(client, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n",
                     seq_start=1001, ack=5001)
    return sink.getvalue()


def test_timeline_merges_an_unsequenced_session_causally():
    with open_bytes(skewed_two_sided_file()) as f:
        session = f.session(7)
        assert not session.sequenced
        merged = list(session.timeline())
        # Causal order: the request precedes the response it caused, even
        # though sorting by timestamp would invert them.
        assert [r.payload[:3] for r in merged] == [b"GET", b"HTT"]
        assert sorted(merged, key=lambda r: r.timestamp) != merged


def test_verify_passes_on_the_merged_example_and_rejects_a_swap():
    with open_text(MERGED_EXAMPLE) as f:
        f.session(1).verify()  # the spec's own sequenced file is valid
    swapped = MERGED_EXAMPLE.splitlines()
    swapped[-2], swapped[-1] = swapped[-1], swapped[-2]
    with open_text("\n".join(swapped)) as f, pytest.raises(zpf.SemanticError, match="ack"):
        f.session(1).verify()


def test_verify_on_an_unsequenced_session_raises():
    with open_bytes(two_session_file()) as f, pytest.raises(zpf.ZpfError, match="nothing to"):
        f.sessions()[0].verify()


def test_stream_of_an_unknown_pid_raises():
    with open_bytes(two_session_file()) as f, pytest.raises(KeyError):
        list(f.sessions()[0].stream(9))


def test_blocks_rewalk_matches_the_original():
    with open_bytes(GOLDEN) as f:
        assert list(f.blocks()) == GOLDEN_BLOCKS


# --- Face handling --------------------------------------------------------------------


def test_face_autodetection(tmp_path: Path):
    binary_path = tmp_path / "cap.zpf"
    binary_path.write_bytes(two_session_file())
    text = io.StringIO()
    zpf.binary_to_jsonl(io.BytesIO(two_session_file()), text)
    jsonl_path = tmp_path / "cap.zpf.jsonl"
    jsonl_path.write_text(text.getvalue(), encoding="utf-8")
    with zpf.open(binary_path) as f:
        assert f.face == "binary"
    with zpf.open(jsonl_path) as f:
        assert f.face == "jsonl"
        assert [s.proto for s in f.sessions()] == ["tcp", "udp"]


def test_jsonl_from_a_binary_stream_left_open():
    text = io.StringIO()
    zpf.binary_to_jsonl(io.BytesIO(two_session_file()), text)
    raw = io.BytesIO(text.getvalue().encode("utf-8"))
    with zpf.open(raw) as f:
        assert f.face == "jsonl"
    raw.seek(0)  # our reader must not have closed the caller's stream
    assert raw.read(1) == b"{"


def test_explicit_face_override_and_errors():
    with open_bytes(GOLDEN, face="binary") as f:
        assert f.face == "binary"
    with pytest.raises(zpf.ZpfError, match="bytes stream"):
        zpf.open(io.StringIO("{}"), face="binary")
    with pytest.raises(ValueError, match="face"):
        open_bytes(GOLDEN, face="xml")
    with pytest.raises(zpf.StructuralError, match="detect"):
        open_bytes(b"\x00" * 32)


def test_non_seekable_source_is_rejected():
    class Pipe(io.RawIOBase):
        def seekable(self) -> bool:
            return False

    with pytest.raises(zpf.ZpfError, match="seekable"):
        zpf.open(Pipe())


# --- Validation modes ---------------------------------------------------------------


def nonconformant_file() -> bytes:
    """Build a structurally fine file whose second record names an unknown session."""
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as w:  # permissive flat writer
        w.write(zpf.FileHeader(tick_hz=1))
        w.write(zpf.Source(source_id=0, kind=zpf.SourceKind.CAPTURE))
        w.write(zpf.Session(session_id=0, proto="tcp"))
        w.write(zpf.Participant(session_id=0, participant_id=0))
        w.write(zpf.Record(session_id=0, sender_pid=0, source_id=0, timestamp=1, payload=b"ok"))
        w.write(zpf.Record(session_id=9, sender_pid=0, source_id=0, timestamp=2, payload=b"bad"))
        w.write(zpf.Record(session_id=0, sender_pid=0, source_id=0, timestamp=3, payload=b"ok2"))
    return sink.getvalue()


def test_lenient_mode_isolates_the_offending_block():
    with open_bytes(nonconformant_file()) as f:
        (session,) = f.sessions()
        assert [r.payload for r in session.records()] == [b"ok", b"ok2"]
        (diag,) = f.diagnostics
        assert diag.category == "nonconformant"
        assert "undeclared session 9" in diag.message
        # blocks() still exposes the isolated record.
        assert sum(isinstance(b, zpf.Record) for b in f.blocks()) == 3


def test_strict_mode_raises_on_the_violation():
    with pytest.raises(zpf.SemanticError, match="undeclared session 9"):
        open_bytes(nonconformant_file(), strict=True)


def unusable_prim_labels_file() -> bytes:
    """Three well-framed records; the last two carry a prim: label they can't hold."""
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as w:  # permissive flat writer
        w.write(zpf.FileHeader(tick_hz=1))
        w.write(zpf.Source(source_id=0, kind=zpf.SourceKind.CAPTURE))
        # A decoder, because content_type belongs to the decoded layer: a
        # transport record MUST NOT carry one, and this file is about the
        # prim: rules rather than that.
        w.write(zpf.Decoder(decoder_id=1, name="probe"))
        w.write(zpf.Session(session_id=0, proto="tcp"))
        w.write(zpf.Participant(session_id=0, participant_id=0))
        for ts, payload, content_type in (
            (1, (1234).to_bytes(4, "little"), "prim:u32"),  # conformant
            (2, b"\x01\x02\x03\x04\x05", "prim:u32"),  # width disagrees with payload_len
            (3, b"\xff", "prim:frobnicate"),  # not in the closed vocabulary
        ):
            w.write(zpf.Record(
                session_id=0, sender_pid=0, source_id=0, decoder_id=1,
                timestamp=ts, payload=payload, content_type=content_type,
            ))
        w.write(zpf.End())
    return sink.getvalue()


def test_an_unusable_prim_label_costs_the_reader_nothing():
    # The spec's fallback: ignore the label, keep the bytes — the reader must
    # not pad, truncate, reinterpret, or (as it once did) drop the record.
    with open_bytes(unusable_prim_labels_file()) as f:
        (session,) = f.sessions()
        records = list(session.records())
        assert [r.payload for r in records] == [
            b"\xd2\x04\x00\x00", b"\x01\x02\x03\x04\x05", b"\xff",
        ]
        assert [r.content_type for r in records] == ["prim:u32", "prim:u32", "prim:frobnicate"]
        # Still reported: a writer MUST NOT emit either record.
        assert [d.category for d in f.diagnostics] == ["nonconformant", "nonconformant"]
        assert "requires payload_len 4, got 5" in f.diagnostics[0].message
        assert "not a legal prim: token" in f.diagnostics[1].message
        # And the label is what gets ignored: the good record reads as a
        # number, the other two as the bytes they are.
        assert [r.content() for r in records] == [
            1234, b"\x01\x02\x03\x04\x05", b"\xff",
        ]


def reserved_flag_bits_file() -> bytes:
    """Build a readable file whose header, session, and a record set a reserved bit."""
    sink = io.BytesIO()
    with zpf.BlockWriter(sink) as w:  # permissive flat writer
        w.write(zpf.FileHeader(tick_hz=1, flags=zpf.FileFlags(0x0002)))
        w.write(zpf.Source(source_id=0, kind=zpf.SourceKind.CAPTURE))
        w.write(zpf.Session(session_id=0, proto="tcp", flags=zpf.SessionFlags(0x0002)))
        w.write(zpf.Participant(session_id=0, participant_id=0))
        w.write(zpf.Record(session_id=0, sender_pid=0, source_id=0, timestamp=1, payload=b"one"))
        w.write(zpf.Record(
            session_id=0, sender_pid=0, source_id=0, timestamp=2, payload=b"two",
            flags=zpf.RecordFlags(0x2000) | zpf.RecordFlags.PSH,
        ))
        w.write(zpf.End())
    return sink.getvalue()


def test_reserved_flag_bits_cost_the_reader_nothing():
    # A reserved bit is extension surface, not a violation: the reader ignores
    # it semantically, keeps it byte-faithfully, and says nothing about it.
    with open_bytes(reserved_flag_bits_file()) as f:
        assert f.header is not None
        assert f.stream_kind(0, 0) == (zpf.SourceKind.CAPTURE, zpf.OutputLayer.TRANSPORT)
        (session,) = f.sessions()
        assert session.proto == "tcp"
        assert len(session.participants) == 1
        records = list(session.records())
        assert [r.payload for r in records] == [b"one", b"two"]
        # The bits are kept as read, not scrubbed — the payload face is faithful.
        assert int(records[1].flags) == 0x2001
        assert zpf.RecordFlags.PSH in records[1].flags
        assert f.diagnostics == []


def test_strict_mode_still_accepts_reserved_flag_bits():
    # Strictness escalates violations; a reserved bit is not one, so the
    # strictest reader available must still read the file without complaint.
    with open_bytes(reserved_flag_bits_file(), strict=True) as f:
        assert f.diagnostics == []
        (session,) = f.sessions()
        assert [r.payload for r in session.records()] == [b"one", b"two"]


def test_strict_mode_still_raises_on_an_unusable_prim_label():
    with pytest.raises(zpf.AdvisoryError, match="requires payload_len 4"):
        open_bytes(unusable_prim_labels_file(), strict=True)


# --- Reading payload content -------------------------------------------------------------


def labelled_file() -> bytes:
    """Build a decode-stage file whose records carry one label of each scheme."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1, produced_by="test 1.0", produced_at=1_719_500_000) as w:
        source = w.add_source("zpf-input", uri="in.zpf")
        http = w.add_decoder("http/1.1")
        smtp = w.add_decoder("smtp")
        with w.begin_session(session_id=7) as s:
            sender = s.participant("client")
            offset = 0
            for decoder, label, payload in (
                (http, "prim:u32", (1234).to_bytes(4, "little")),
                (http, "mime:application/json", b'{"ok": true}'),
                (http, "mime:text/plain; charset=utf-8", b"hello"),
                (http, "dec:request", b"GET /"),
                (smtp, "dec:request", b"MAIL FROM"),  # same token, other decoder
                (http, "dec:unregistered", b"?"),
                (http, "x-private:thing", b"opaque"),
            ):
                # A decode stage's records must cite the input ranges they
                # were built from; spans are what make this file a decode
                # stage rather than a pass-through.
                span = zpf.Span(
                    source_id=source.source_id, session_id=7, participant_id=0,
                    off_start=offset, off_end=offset + len(payload),
                )
                offset += len(payload)
                s.record(sender, ts=0, payload=payload, source=source,
                         decoder=decoder, content_type=label, spans=(span,))
    return sink.getvalue()


def content_registry() -> zpf.ContentRegistry:
    registry = zpf.ContentRegistry()
    registry.register_mime("application/json", json.loads)
    registry.register_mime("text/plain", lambda payload: payload.decode("ascii"))
    registry.register_dec("http/1.1", "request", lambda payload: ("http", payload))
    registry.register_dec("smtp", "request", lambda payload: ("smtp", payload))
    return registry


def test_content_without_a_registry_is_exactly_the_records_own():
    with open_bytes(labelled_file()) as f:
        records = list(f.session(7).records())
        assert [f.content(r) for r in records] == [r.content() for r in records]
        assert [f.content(r) for r in records[:2]] == [1234, b'{"ok": true}']


def test_a_registry_resolves_the_advisory_schemes():
    with open_bytes(labelled_file(), content=content_registry()) as f:
        assert [f.content(r) for r in f.session(7).records()] == [
            1234,  # prim: is built in, and never routed through the registry
            {"ok": True},  # mime:, by media type
            "hello",  # mime:, parameters ignored when matching
            ("http", b"GET /"),  # dec:, namespaced by the decoder's name...
            ("smtp", b"MAIL FROM"),  # ...so the same token differs per decoder
            b"?",  # dec: token nobody registered: the payload, unchanged
            b"opaque",  # unknown scheme: opaque, per the format
        ]


def test_a_registry_cannot_override_the_normative_scheme():
    registry = zpf.ContentRegistry()
    registry.register_mime("application/json", json.loads)
    # prim: is fully spec-defined, so a handler for it is not even expressible;
    # the built-in decode answers, and an unusable prim: label still falls back.
    with open_bytes(unusable_prim_labels_file(), content=registry) as f:
        assert [f.content(r) for r in f.session(0).records()] == [
            1234, b"\x01\x02\x03\x04\x05", b"\xff",
        ]


def test_strict_content_raises_only_where_nothing_could_interpret():
    with open_bytes(labelled_file(), content=content_registry()) as f:
        records = list(f.session(7).records())
        for record in records[:5]:  # prim: + every registered handler
            f.content(record, strict=True)
        for record in records[5:]:  # unregistered dec:, unknown scheme
            with pytest.raises(zpf.ContentError):
                f.content(record, strict=True)


def test_a_handlers_exception_reaches_the_caller():
    # A handler that fails is a bug or corrupt input, not a fallback: the
    # library must not quietly hand back bytes and hide it.
    class Boom(Exception):
        pass

    def explode(payload: bytes) -> object:
        raise Boom(payload)

    registry = zpf.ContentRegistry()
    registry.register_mime("application/json", explode)
    with open_bytes(labelled_file(), content=registry) as f:
        record = next(
            r for r in f.session(7).records() if r.content_type == "mime:application/json"
        )
        with pytest.raises(Boom):
            f.content(record)
        with pytest.raises(Boom):
            f.content(record, strict=True)  # nor does strict= make it a fallback


def test_a_dec_label_needs_the_files_decoder_name():
    registry = zpf.ContentRegistry()
    registry.register_dec("http/1.1", "request", lambda payload: ("http", payload))
    with open_bytes(labelled_file(), content=registry) as f:
        record = next(r for r in f.session(7).records() if r.content_type == "dec:request")
        assert f.content(record) == ("http", b"GET /")
        # Without a decoder_id there is no namespace to resolve the token in,
        # and an id this file never declared resolves to no name either.
        assert f.content(dataclasses.replace(record, decoder_id=None)) == b"GET /"
        assert f.content(dataclasses.replace(record, decoder_id=99)) == b"GET /"


# --- Truncation and completeness -------------------------------------------------------


def test_truncated_file_keeps_prior_sessions_readable():
    data = two_session_file()
    with open_bytes(data[:-10]) as f:
        assert f.truncated
        assert not f.complete
        tcp, udp = f.sessions()
        assert [r.payload for r in tcp.records()] == [b"t1", b"t2", b"t3"]
        assert f.diagnostics[-1].category == "truncated"
    with pytest.raises(zpf.TruncatedError):
        open_bytes(data[:-10], strict=True)


def test_complete_flag_reflects_the_end_block():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1) as w:
        w.add_source("capture")
    with open_bytes(sink.getvalue()) as f:
        assert f.complete


def test_closed_reader_refuses_lazy_reads():
    f = open_bytes(two_session_file())
    (tcp, _) = f.sessions()
    f.close()
    with pytest.raises(zpf.ZpfError, match="closed"):
        list(tcp.records())


# --- The checker keeps going after an isolated block (refactor regression) --------------


def test_checker_state_survives_an_isolated_violation():
    checker = zpf.ConformanceChecker()
    checker.observe(zpf.FileHeader(tick_hz=1))
    checker.observe(zpf.Source(source_id=0, kind=zpf.SourceKind.CAPTURE))
    checker.observe(zpf.Session(session_id=0))
    bad = zpf.Participant(session_id=1, participant_id=0)  # undeclared session
    with pytest.raises(zpf.SemanticError):
        checker.observe(bad)
    # The failed block left no trace: the same pid can be declared properly.
    checker.observe(zpf.Participant(session_id=0, participant_id=0))
    out_of_order = zpf.Record(
        session_id=0, sender_pid=0, source_id=0, timestamp=0, seq_start=100
    )
    checker.observe(out_of_order)
    with pytest.raises(zpf.SemanticError):
        checker.observe(
            zpf.Record(session_id=0, sender_pid=0, source_id=0, timestamp=1, seq_start=50)
        )
    # The rejected record did not advance the participant's seq cursor.
    checker.observe(
        zpf.Record(session_id=0, sender_pid=0, source_id=0, timestamp=2, seq_start=100)
    )


def test_index_offsets_are_exact():
    # The reader computes record offsets from cached block bytes; verify
    # against a hand-walk of the frames.
    data = two_session_file()
    offsets = []
    pos = 0
    while pos < len(data):
        block_type, _res, length = _frame.FRAME.unpack_from(data, pos)
        if block_type == _frame.BT_RECORD:
            offsets.append(pos)
        pos += _frame.FRAME_SIZE + length
    with open_bytes(data) as f:
        tcp, udp = f.sessions()
        got = [r.payload for r in tcp.records()] + [r.payload for r in udp.records()]
        assert got == [b"t1", b"t2", b"t3", b"u1", b"u2"]
        assert len(offsets) == 5


# --- Decoded offset spaces ---------------------------------------------------

VECTORS = pathlib.Path(__file__).parent / "vectors"


def test_ranges_place_a_decoded_record_positionally():
    # A Record block carries no offset field, so a decoded record's place in
    # its own stream is implied by the concatenation of the preceding
    # payloads. ranges() is the only way to recover it.
    with zpf.open(VECTORS / "chain/decoded.zpf") as f:
        session = f.session(7)
        assert session.layer(0) is zpf.OutputLayer.DECODED
        for pid in (0, 1):
            records = list(session.stream(pid))
            ranges = session.ranges(pid)
            assert len(ranges) == len(records)
            cursor = 0
            for record, (start, end) in zip(records, ranges, strict=True):
                assert (start, end) == (cursor, cursor + len(record.payload))
                cursor = end


def test_ranges_are_cached_across_session_views():
    # SessionReader is rebuilt per call, so the table has to live on the
    # index or the first pass would be paid again on every lookup.
    with zpf.open(VECTORS / "chain/decoded.zpf") as f:
        first = f.session(7).ranges(0)
        assert f.session(7).ranges(0) is first


@pytest.mark.parametrize(
    "vector",
    [
        "chain/decoded.zpf",
        # The positional walk is written twice — record_ranges and the
        # cursor inside StreamView.datagrams — so this test is the only
        # thing holding them together. On a file with no Discontinuity it
        # holds them together at the easy case: both would still agree with
        # widths ignored. These two make it bite.
        "discontinuity-known-width/discontinuity-known-width.zpf",
        "passthrough-discontinuity/passthrough-discontinuity.zpf",
    ],
)
def test_ranges_agree_with_a_naive_datagram_walk(vector: str):
    with zpf.open(VECTORS / vector) as f:
        for session in f.sessions():
            for view in session.reassemble():
                pid = view.participant.participant_id
                walked = [(d.off_start, d.off_end) for d in view.datagrams()]
                assert list(session.ranges(pid)) == walked


def test_a_declared_width_moves_the_records_after_it():
    """The one number that proves the phase.

    ``discontinuity-known-width`` exists because a reader that skips the
    block, or reads it and ignores ``width``, computes ``[50, 80)`` for the
    second record where a correct one computes ``[75, 105)``. Nothing in the
    ``accept`` tier catches the difference: the file reads cleanly and
    projects correctly either way, because no vector test asks for a range.
    """
    path = VECTORS / "discontinuity-known-width/discontinuity-known-width.zpf"
    with zpf.open(path) as f:
        session = f.session(9)
        assert session.ranges(0) == ((0, 50), (75, 105))
        # The file's own Session End declares the input stream 105 long, and
        # this decoder is byte-for-byte on its input, so the output space
        # reaching 105 is the file agreeing with itself.
        assert session.end is not None
        assert session.end.input_extents[0].extent == 105


def test_an_unknown_width_leaves_the_records_where_they_were():
    path = VECTORS / "discontinuity-unknown-width/discontinuity-unknown-width.zpf"
    with zpf.open(path) as f:
        assert f.session(7).ranges(0) == ((0, 50), (50, 80))


def test_a_break_is_indexed_but_kept_out_of_the_record_stream():
    """``stream`` stays records-only; ``stream_blocks`` is the walked sequence."""
    path = VECTORS / "discontinuity-known-width/discontinuity-known-width.zpf"
    with zpf.open(path) as f:
        session = f.session(9)
        assert [type(b).__name__ for b in session.stream(0)] == ["Record", "Record"]
        assert [type(b).__name__ for b in session.stream_blocks(0)] == [
            "Record",
            "Discontinuity",
            "Record",
        ]


def test_a_raw_stream_is_not_a_decoded_one():
    with zpf.open(VECTORS / "chain/raw.zpf") as f:
        session = f.session(7)
        assert session.layer(0) is zpf.OutputLayer.TRANSPORT


def test_stream_kind_reports_both_axes_independently():
    """The table the two-axis model exists to make expressible.

    ``proxy-decoded`` is the cell that had no honest encoding before 0.15: a
    decoded stream whose records reference a *capture* Source, because a
    TLS-terminating proxy has no predecessor `.zpf` and never will.
    ``reassembler-declared`` is capture-sourced and transport, with a
    Decoder — the shape a reader that infers the layer from `decoder_id`
    gets wrong.
    """
    cases = {
        "raw-minimal": (zpf.SourceKind.CAPTURE, zpf.OutputLayer.TRANSPORT),
        "proxy-decoded": (zpf.SourceKind.CAPTURE, zpf.OutputLayer.DECODED),
        "reassembler-declared": (zpf.SourceKind.CAPTURE, zpf.OutputLayer.TRANSPORT),
        "sessionization-stage": (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.TRANSPORT),
        "decoded-basic": (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.DECODED),
    }
    for name, expected in cases.items():
        with zpf.open(VECTORS / name / f"{name}.zpf") as f:
            session = f.sessions()[0]
            pid = session.participants[0].participant_id
            assert f.stream_kind(session.session_id, pid) == expected, name


def test_one_file_may_hold_streams_at_different_positions():
    """``tunnel/`` walks the chain, and no two hops agree on both axes."""
    cases = {
        "outer": (zpf.SourceKind.CAPTURE, zpf.OutputLayer.TRANSPORT),
        "packets": (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.DECODED),
        "inner": (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.TRANSPORT),
        "http": (zpf.SourceKind.ZPF_INPUT, zpf.OutputLayer.DECODED),
    }
    for member, expected in cases.items():
        with zpf.open(VECTORS / "tunnel" / f"{member}.zpf") as f:
            session = f.sessions()[0]
            pid = session.participants[0].participant_id
            assert f.stream_kind(session.session_id, pid) == expected, member


def test_the_layer_is_resolved_once_per_participant():
    """The cache the per-participant consistency rule licenses."""
    with zpf.open(VECTORS / "chain/decoded.zpf") as f:
        session = f.session(7)
        first = session.layer(0)
        assert session.layer(0) is first
        assert f._sessions[7].layers[0] is first


# --- as_datetime (#53) ----------------------------------------------------------------


def test_as_datetime_reads_a_record_timestamp_end_to_end():
    """The whole point: a record's ticks, as wall-clock time."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000) as writer:
        writer.add_source("capture", uri="c.pcap")
        with writer.begin_session(proto="tcp") as session:
            alice = session.participant("alice", isn=0)
            session.record(alice, ts=1_786_646_192_538_796, payload=b"hi", seq_start=1)
    with zpf.open(io.BytesIO(sink.getvalue())) as reader:
        (session,) = reader.sessions()
        (record,) = session.records()
        when = zpf.as_datetime(record.timestamp, reader.header)
    assert when == datetime(2026, 8, 13, 18, 36, 32, 538796, tzinfo=UTC)
    assert when.tzinfo is UTC


def test_as_datetime_counts_from_the_declared_epoch():
    """`time_epoch` is the origin the ticks are counted from, also in ticks."""
    header = zpf.FileHeader(tick_hz=1_000_000, time_epoch=1_700_000_000_000_000)
    assert zpf.as_datetime(5_000_000, header) == datetime(
        2023, 11, 14, 22, 13, 25, tzinfo=UTC
    )


def test_as_datetime_treats_an_absent_epoch_as_zero():
    """`time_epoch=None` means the default origin, not a missing value."""
    header = zpf.FileHeader(tick_hz=1_000_000)
    assert header.time_epoch is None
    assert zpf.as_datetime(0, header) == datetime(1970, 1, 1, tzinfo=UTC)


def test_as_datetime_passes_none_through_for_an_absent_optional():
    """So `ts_first`, which is optional, needs no guard at the call site."""
    header = zpf.FileHeader(tick_hz=1_000_000)
    assert zpf.as_datetime(None, header) is None


def test_as_datetime_handles_a_timestamp_before_the_epoch():
    """Timestamps are signed; a negative one is not an error."""
    header = zpf.FileHeader(tick_hz=1_000_000)
    assert zpf.as_datetime(-1_500_000, header) == datetime(
        1969, 12, 31, 23, 59, 58, 500000, tzinfo=UTC
    )


def test_as_datetime_is_exact_at_a_nanosecond_tick_hz():
    """The reason for integer arithmetic rather than one float division.

    At `tick_hz=1e9` the tick count exceeds float64's exactly-representable
    integer range, so `(epoch + value) / tick_hz` lands on the wrong
    microsecond for a few percent of values — this one included.
    """
    header = zpf.FileHeader(tick_hz=1_000_000_000)
    value = 1_734_683_192_655_088_527
    naive_float = datetime.fromtimestamp(value / header.tick_hz, tz=UTC)
    exact = round(Fraction(value, header.tick_hz) * 1_000_000)
    assert zpf.as_datetime(value, header) == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=exact
    )
    assert zpf.as_datetime(value, header) != naive_float  # the bug this avoids

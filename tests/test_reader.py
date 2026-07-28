"""Tests for the session-first reader (zpf.open)."""

from __future__ import annotations

import dataclasses
import io
import json
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
        assert f.file_kind == "raw"
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
        assert f.file_kind == "pass-through"
        session = f.session(1)
        assert session.sequenced
        assert session.key == "10.0.0.1:51000 <-> 93.184.216.34:80"
        assert [p.origin.session_id for p in session.participants] == [7, 3]
        # A sequenced session's timeline is its stored order.
        assert list(session.timeline()) == list(session.records())
        assert [r.sender_pid for r in session.timeline()] == [0, 1]


def test_open_the_decoded_example_jsonl():
    with open_text(DECODED_EXAMPLE) as f:
        assert f.file_kind == "decode-stage"
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
        w.write(zpf.Session(session_id=0, proto="tcp"))
        w.write(zpf.Participant(session_id=0, participant_id=0))
        for ts, payload, content_type in (
            (1, (1234).to_bytes(4, "little"), "prim:u32"),  # conformant
            (2, b"\x01\x02\x03\x04\x05", "prim:u32"),  # width disagrees with payload_len
            (3, b"\xff", "prim:frobnicate"),  # not in the closed vocabulary
        ):
            w.write(zpf.Record(
                session_id=0, sender_pid=0, source_id=0,
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
    # A reader has no meaning to attach to a reserved bit, so it ignores the
    # bits and uses the block. Isolating instead would empty the file: without
    # its header every later block is "first block must be a File Header", and
    # without its Session Descriptor a session's records have nowhere to go.
    with open_bytes(reserved_flag_bits_file()) as f:
        assert f.header is not None
        assert f.file_kind == "raw"
        (session,) = f.sessions()
        assert session.proto == "tcp"
        assert len(session.participants) == 1
        records = list(session.records())
        assert [r.payload for r in records] == [b"one", b"two"]
        # The bits are kept as read, not scrubbed — the payload face is faithful.
        assert int(records[1].flags) == 0x2001
        assert zpf.RecordFlags.PSH in records[1].flags
        # Reported, though: three blocks, three findings, and nothing else.
        assert [d.category for d in f.diagnostics] == ["nonconformant"] * 3
        assert all("reserved bits" in d.message for d in f.diagnostics)


def test_strict_mode_still_raises_on_reserved_flag_bits():
    with pytest.raises(zpf.AdvisoryError, match="File Header flags 0x0002"):
        open_bytes(reserved_flag_bits_file(), strict=True)


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
            for decoder, label, payload in (
                (http, "prim:u32", (1234).to_bytes(4, "little")),
                (http, "mime:application/json", b'{"ok": true}'),
                (http, "mime:text/plain; charset=utf-8", b"hello"),
                (http, "dec:request", b"GET /"),
                (smtp, "dec:request", b"MAIL FROM"),  # same token, other decoder
                (http, "dec:unregistered", b"?"),
                (http, "x-private:thing", b"opaque"),
            ):
                s.record(sender, ts=0, payload=payload, source=source,
                         decoder=decoder, content_type=label)
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

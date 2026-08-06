"""Tests for the ergonomic handle-based writer (zpf.create)."""

from __future__ import annotations

import io
import json

import pytest
from _migration import pending_version_gate
from test_golden import GOLDEN
from test_jsonl import DECODED_EXAMPLE, MERGED_EXAMPLE

import zpf
from zpf.jsonl import block_to_obj


@pending_version_gate
def test_handle_api_reproduces_the_golden_file():
    sink = io.BytesIO()
    writer = zpf.create(sink, tick_hz=1_000_000)
    writer.add_source("capture", uri="sideA.pcap", source_id=1)
    session = writer.begin_session(proto="tcp", session_id=7)
    alice = session.participant("10.0.0.1:51000", isn=1000)
    session.record(
        alice,
        ts=1000,
        payload=b"GET / HTTP/1.1\r\n\r\n",
        flags=zpf.RecordFlags.PSH,
        seq_start=1001,
        ack=5001,
    )
    writer.close(end=False)  # the spec's worked example has no End block
    assert sink.getvalue() == GOLDEN


def test_handle_api_reproduces_the_decoded_example():
    sink = io.BytesIO()
    writer = zpf.create(
        sink, tick_hz=1_000_000, produced_by="zpf-decode 0.4", produced_at=1_719_500_000
    )
    raw = writer.add_source("zpf-input", uri="raw.zpf", digest="sha256:9f2c…", source_id=1)
    http = writer.add_decoder(
        "http/1.1", version="0.4", params_digest="sha256:00ab…", decoder_id=1
    )
    session = writer.begin_session(proto="http", session_id=7)
    client = session.participant("10.0.0.1:51000")
    server = session.participant("93.184.216.34:80")
    session.record(
        client,
        ts=1000,
        payload=b"request",
        decoder=http,
        content_type="dec:request",
        spans=(zpf.Span(source_id=1, session_id=7, participant_id=0, off_start=0, off_end=18),),
    )
    session.record(
        server,
        ts=995,
        payload=b"response",
        decoder=http,
        content_type="dec:response",
        spans=(zpf.Span(source_id=1, session_id=7, participant_id=1, off_start=0, off_end=100),),
    )
    writer.undecoded(raw, 7, 1, 100, 139, reason="undecodable", decoder=http)
    writer.close(end=False)
    written = [block_to_obj(b) for b in zpf.BlockReader(io.BytesIO(sink.getvalue()))]
    expected = [json.loads(line) for line in DECODED_EXAMPLE.splitlines() if line.strip()]
    assert written == expected


def test_handle_api_reproduces_the_merged_example():
    sink = io.BytesIO()
    writer = zpf.create(
        sink, tick_hz=1_000_000, produced_by="zpf-merge 1.2", produced_at=1_719_510_000
    )
    side_a = writer.add_source("zpf-input", uri="sideA.zpf", digest="sha256:11aa…", source_id=1)
    side_b = writer.add_source("zpf-input", uri="sideB.zpf", digest="sha256:22bb…", source_id=2)
    session = writer.begin_session(
        proto="tcp", key="10.0.0.1:51000 <-> 93.184.216.34:80", sequenced=True, session_id=1
    )
    client = session.participant(
        "10.0.0.1:51000", isn=1000,
        origin=zpf.Origin(source_id=1, session_id=7, participant_id=0),
    )
    server = session.participant(
        "93.184.216.34:80", isn=5000,
        origin=zpf.Origin(source_id=2, session_id=3, participant_id=0),
    )
    session.record(
        client, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n",
        source=side_a, seq_start=1001, ack=5001,
    )
    session.record(
        server, ts=995, payload=b"HTTP/1.1 200 OK\r\n...", source=side_b,
        seq_start=5001, ack=1019,
    )
    writer.close(end=False)
    written = [block_to_obj(b) for b in zpf.BlockReader(io.BytesIO(sink.getvalue()))]
    expected = [json.loads(line) for line in MERGED_EXAMPLE.splitlines() if line.strip()]
    # The example's response payload is elided in the spec; align it.
    expected[-1]["payload"] = written[-1]["payload"]
    assert written == expected


def test_end_block_on_clean_exit_only():
    clean = io.BytesIO()
    with zpf.create(clean, tick_hz=1) as writer:
        writer.add_source("capture")
    reader = zpf.BlockReader(io.BytesIO(clean.getvalue()))
    list(reader)
    assert reader.complete

    failed = io.BytesIO()
    with pytest.raises(RuntimeError), zpf.create(failed, tick_hz=1) as writer:
        writer.add_source("capture")
        raise RuntimeError("boom")
    reader = zpf.BlockReader(io.BytesIO(failed.getvalue()))
    list(reader)
    assert not reader.complete  # honestly incomplete


def test_session_context_manager_auto_ends():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1) as writer:
        with writer.begin_session() as session:
            session.participant("alice")
        with writer.begin_session() as session:
            session.end(reason="fin")  # explicit end; the context must not double-end
    blocks = list(zpf.BlockReader(io.BytesIO(sink.getvalue())))
    session_ends = [b for b in blocks if isinstance(b, zpf.SessionEnd)]
    assert [b.reason for b in session_ends] == [None, "fin"]


def test_ended_session_refuses_use():
    with zpf.create(io.BytesIO(), tick_hz=1) as writer:
        writer.add_source("capture")
        session = writer.begin_session()
        sender = session.participant("alice")
        session.end(reason="timeout")
        with pytest.raises(zpf.SemanticError, match="after its Session End"):
            session.record(sender, ts=0)


def test_auto_ids_and_explicit_overrides():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1) as writer:
        assert writer.add_source("capture").source_id == 0
        assert writer.add_source("capture", source_id=7).source_id == 7
        assert writer.add_source("capture").source_id == 1
        with pytest.raises(zpf.SemanticError, match="declared twice"):
            writer.add_source("capture", source_id=7)
        session = writer.begin_session()
        assert session.session_id == 0
        assert writer.begin_session(session_id=5).session_id == 5
        assert writer.begin_session().session_id == 1
        assert session.participant("a").pid == 0
        assert session.participant("b", pid=4).pid == 4
        assert session.participant("c").pid == 1


def test_single_source_defaulting():
    with zpf.create(io.BytesIO(), tick_hz=1) as writer:
        source = writer.add_source("capture")
        session = writer.begin_session()
        sender = session.participant("alice")
        session.record(sender, ts=0, payload=b"x")  # defaults to the only source
        writer.add_source("capture")
        with pytest.raises(zpf.ZpfError, match="pass source="):
            session.record(sender, ts=1, payload=b"y")
        session.record(sender, ts=1, payload=b"y", source=source)


def test_sender_must_belong_to_the_session():
    with zpf.create(io.BytesIO(), tick_hz=1) as writer:
        writer.add_source("capture")
        first = writer.begin_session()
        stranger = first.participant("alice")
        second = writer.begin_session()
        with pytest.raises(zpf.ZpfError, match="not this session"):
            second.record(stranger, ts=0)


def test_out_of_order_seq_start_is_refused():
    with zpf.create(io.BytesIO(), tick_hz=1) as writer:
        writer.add_source("capture")
        session = writer.begin_session(proto="tcp")
        sender = session.participant("alice", isn=0xFFFF_FFE0)
        session.record(sender, ts=0, payload=b"aa", seq_start=0xFFFF_FFF0)
        session.record(sender, ts=1, payload=b"bb", seq_start=0x10)  # wrap-legal
        with pytest.raises(zpf.SemanticError, match="seq_start order"):
            session.record(sender, ts=2, payload=b"cc", seq_start=0xFFFF_FFF0)


def test_jsonl_face_matches_the_binary_conversion():
    def build(writer: zpf.FileWriter) -> None:
        writer.add_source("capture", uri="chat.pcap")
        with writer.begin_session(proto="irc") as session:
            alice = session.participant("alice")
            session.record(alice, ts=2000, payload=b"hi, all!")
            session.name(alice, "Alice", kind="nick")
            session.end(reason="timeout")

    binary = io.BytesIO()
    with zpf.create(binary, tick_hz=1_000_000) as writer:
        build(writer)
    text = io.StringIO()
    with zpf.create(text, tick_hz=1_000_000, face="jsonl") as writer:
        build(writer)
    converted = io.StringIO()
    zpf.binary_to_jsonl(io.BytesIO(binary.getvalue()), converted)
    assert text.getvalue() == converted.getvalue()


def test_escape_hatches_are_checked():
    with zpf.create(io.BytesIO(), tick_hz=1) as writer:
        writer.write_custom(pen=32473, subtype=7, payload=b"vend")
        writer.add_source("capture")
        session = writer.begin_session()
        sender = session.participant("alice")
        # Full-control Record with an option the keyword API omits.
        writer.write_block(
            zpf.Record(
                session_id=session.session_id,
                sender_pid=sender.pid,
                source_id=0,
                timestamp=5,
                ts_first=3,
                payload=b"x",
            )
        )
        with pytest.raises(zpf.SemanticError, match="undeclared session"):
            writer.write_block(zpf.SessionEnd(session_id=99))


def test_manual_end_block_is_not_doubled():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1) as writer:
        writer.write_block(zpf.End(comment="bye"))
    blocks = list(zpf.BlockReader(io.BytesIO(sink.getvalue())))
    assert blocks[-1] == zpf.End(comment="bye")
    assert sum(isinstance(b, zpf.End) for b in blocks) == 1


def test_invalid_face_is_rejected():
    with pytest.raises(ValueError, match="face"):
        zpf.create(io.BytesIO(), tick_hz=1, face="xml")  # type: ignore[arg-type]


def test_closed_writer_refuses_use():
    writer = zpf.create(io.BytesIO(), tick_hz=1)
    writer.close()
    assert writer.closed
    with pytest.raises(zpf.ZpfError, match="closed"):
        writer.add_source("capture")

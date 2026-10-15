"""Tests for the sessionization stage: a reassembler over a .zpf, fanning out (#76)."""

from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import zpf

TUNNEL = Path(__file__).parent / "vectors" / "tunnel"
PACKETS = TUNNEL / "packets.zpf"
INNER = TUNNEL / "inner.zpf"


@dataclass(frozen=True)
class Flow:
    """What the test's own packet parser reads out of one inner IPv4 + TCP packet."""

    key: str
    src: str
    seq: int
    ack: int
    flags: int
    payload: bytes


def parse_tcp(packet: bytes) -> Flow:
    """Parse one IPv4 + TCP packet — the caller's job, never the library's."""
    ihl = (packet[0] & 0x0F) * 4
    src, dst = packet[12:16], packet[16:20]
    sport, dport, seq, ack, offset_flags = struct.unpack("!HHIIH", packet[ihl : ihl + 14])
    data = ihl + (offset_flags >> 12) * 4
    host = ".".join
    a = f"{host(map(str, src))}:{sport}"
    b = f"{host(map(str, dst))}:{dport}"
    return Flow(f"{a} -> {b}", a, seq, ack, offset_flags & 0x3F, packet[data:])


TCP_PSH = 0x08


def sessionize_tunnel(sink: io.BytesIO, *, per_flow: bool) -> None:
    """Rebuild tunnel/inner.zpf from tunnel/packets.zpf.

    ``per_flow`` writes each session whole, as the vendored file is laid out;
    otherwise records go out as the packets arrive, interleaving the sessions.
    """
    with zpf.sessionize_stage(
        PACKETS, sink,
        decoder=("tcp-reassembly", "1.1"), params_digest="sha256:2f60",
        produced_by="zpf-sessionize 1.0", produced_at=1719700100,
        input_ref=zpf.InputRef(uri="packets.zpf"),
    ) as stage:
        found: list[tuple[zpf.InputStream, zpf.reassembly.Datagram, Flow]] = []
        for stream in stage.streams():
            for datagram in stream.datagrams():
                found.append((stream, datagram, parse_tcp(datagram.data)))
        ids = {"10.8.0.2:44300 -> 10.8.0.9:80": 10, "10.8.0.2:44301 -> 10.8.0.9:53": 11}
        if per_flow:
            found.sort(key=lambda item: ids[item[2].key])
        for stream, datagram, flow in found:
            session = stage.session(flow.key, proto="tcp", session_id=ids[flow.key])
            sender = session.participant(flow.src, isn=1000 if ids[flow.key] == 10 else 5000)
            session.record(
                sender, flow.payload,
                cites=stream.cite(datagram.off_start, datagram.off_end),
                hints=zpf.Hints(seq_start=flow.seq, ack=flow.ack),
                flags=zpf.RecordFlags.PSH if flow.flags & TCP_PSH else 0,
            )
            if per_flow and flow.payload.startswith(b"b"):  # flow A's last packet
                session.end()


def projection(data: bytes) -> list[dict[str, Any]]:
    """Project a file to JSON objects, with Source and Decoder ids renumbered from 1.

    The vendored file numbers them from 1 and this writer from 0. Ids are the
    writer's to choose, and nothing else differs, so they are the one thing
    normalised.
    """
    with zpf.open(io.BytesIO(data)) as reader:
        objects = [json.loads(zpf.dumps_block(block)) for block in reader.blocks()]
    base = min(o["source_id"] for o in objects if o["type"] == "source")
    return [_renumber(o, 1 - base) for o in objects]


def _renumber(value: Any, shift: int) -> Any:
    if isinstance(value, dict):
        return {
            k: (v + shift if k in {"source_id", "decoder_id"} and isinstance(v, int)
                else _renumber(v, shift))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_renumber(v, shift) for v in value]
    return value


# --- The acceptance test: tunnel/inner.zpf, rebuilt -------------------------------


def test_the_tunnels_inner_hop_is_rebuilt_from_its_packets():
    """#76's acceptance: the vendored inner.zpf, from packets.zpf and the API alone."""
    sink = io.BytesIO()
    sessionize_tunnel(sink, per_flow=True)
    assert projection(sink.getvalue()) == projection(INNER.read_bytes())


def test_written_as_the_packets_arrive_the_flows_interleave_and_still_conform():
    sink = io.BytesIO()
    sessionize_tunnel(sink, per_flow=False)
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert out.complete
        assert out.diagnostics == []
        assert [s.session_id for s in out.sessions()] == [10, 11]
        for session in out.sessions():
            (end,) = [b for b in out.blocks()
                      if isinstance(b, zpf.SessionEnd) and b.session_id == session.session_id]
            # Under fan-out each session declares the whole input stream.
            assert [e.extent for e in end.input_extents] == [210]
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), PACKETS) == []


# --- Coalescing across input units (#152) -----------------------------------------


def unit_sequence_of_packets() -> bytes:
    """Build a packet stream declared ``units``: flow A at 1001 and 1041, flow B between.

    The shape ``sessionized-across-units`` describes, whose input the
    vectors do not ship. Payloads stand for packets; the test knows what is
    in each, so no parsing is needed.
    """
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000, produced_by="t", produced_at=1) as writer:
        outer = writer.add_source("zpf-input", uri="outer.zpf", digest="sha256:00")
        decrypt = writer.add_decoder("wireguard-decrypt", output_layer=zpf.OutputLayer.DECODED)
        session = writer.begin_session(proto="ip", session_id=5)
        tunnel = session.participant("10.8.0.2", adjacency=zpf.Adjacency.UNITS)
        for k, payload in enumerate((b"A" * 40, b"C" * 20, b"B" * 30)):
            session.record(
                tunnel, ts=1000 + 100 * k, payload=payload,
                decoded=zpf.Decoded(
                    decoder=decrypt, content_type=zpf.NET_IP,
                    spans=[zpf.Span(source_id=outer.source_id, session_id=1,
                                    participant_id=0, off_start=80 * k, off_end=80 * k + 80)],
                ),
            )
    return sink.getvalue()


def test_a_record_with_seq_start_may_cite_two_input_units():
    raw = unit_sequence_of_packets()
    sink = io.BytesIO()
    with zpf.sessionize_stage(
        io.BytesIO(raw), sink, decoder="tcp-reassembly", produced_by="t", produced_at=2
    ) as stage:
        (stream,) = stage.streams()
        flow_a = stage.session("a", proto="tcp")
        flow_b = stage.session("b", proto="tcp")
        flow_a.record(
            flow_a.participant("10.8.0.2:44300", isn=1000), b"A" * 40 + b"B" * 30,
            cites=[stream.cite(0, 40), stream.cite(60, 90)],
            hints=zpf.Hints(seq_start=1001),
        )
        flow_b.record(flow_b.participant("10.8.0.2:44301", isn=5000), b"C" * 20,
                      cites=stream.cite(40, 60), hints=zpf.Hints(seq_start=5001))
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert out.diagnostics == []
        (coalesced,) = out.session(flow_a.session_id).records()
        assert len(coalesced.spans) == 2
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


def test_without_seq_start_a_record_cites_one_range():
    with zpf.sessionize_stage(
        io.BytesIO(unit_sequence_of_packets()), io.BytesIO(),
        decoder="udp-sessionize", produced_by="t", produced_at=2,
    ) as stage:
        (stream,) = stage.streams()
        flow = stage.session("a", proto="udp")
        sender = flow.participant("10.8.0.2:5353")
        with pytest.raises(zpf.SemanticError, match="must carry seq_start"):
            flow.record(sender, b"x", cites=[stream.cite(0, 40), stream.cite(60, 90)])
        flow.record(sender, b"x", cites=stream.cite(0, 40))


def test_a_record_must_cite_something():
    with zpf.sessionize_stage(
        io.BytesIO(unit_sequence_of_packets()), io.BytesIO(),
        decoder="tcp-reassembly", produced_by="t", produced_at=2,
    ) as stage:
        flow = stage.session("a", proto="tcp")
        with pytest.raises(zpf.SemanticError, match="must cite the input range"):
            flow.record(flow.participant("x"), b"x", cites=[], hints=zpf.Hints(seq_start=1))


# --- Coverage across sessions -----------------------------------------------------


def test_what_no_session_cites_is_auto_filled_once_for_the_input_stream():
    raw = unit_sequence_of_packets()
    sink = io.BytesIO()
    with zpf.sessionize_stage(
        io.BytesIO(raw), sink, decoder="tcp-reassembly", produced_by="t", produced_at=2
    ) as stage:
        (stream,) = stage.streams()
        flow = stage.session("a", proto="tcp")
        sender = flow.participant("10.8.0.2:44300", isn=1000)
        flow.record(sender, b"A" * 40, cites=stream.cite(0, 40), hints=zpf.Hints(seq_start=1001))
        flow.record(sender, b"B" * 30, cites=stream.cite(60, 90), hints=zpf.Hints(seq_start=1041))
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        marks = [(u.off_start, u.off_end, u.reason) for u in out.undecoded]
    assert marks == [(40, 60, "skipped")]
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


def test_an_explicit_marker_counts_toward_coverage():
    raw = unit_sequence_of_packets()
    sink = io.BytesIO()
    with zpf.sessionize_stage(
        io.BytesIO(raw), sink, decoder="tcp-reassembly", produced_by="t", produced_at=2
    ) as stage:
        (stream,) = stage.streams()
        stage.undecoded(stream, 0, 90, reason="not-tcp", reason_class="bytes")
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert [(u.reason, u.reason_class) for u in out.undecoded] == [("not-tcp", "bytes")]
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


# --- Sessions and participants on demand ------------------------------------------


def test_sessions_and_participants_are_cached_by_key_and_endpoint():
    with zpf.sessionize_stage(
        io.BytesIO(unit_sequence_of_packets()), io.BytesIO(),
        decoder="tcp-reassembly", produced_by="t", produced_at=2,
    ) as stage:
        first = stage.session("a", proto="tcp")
        assert stage.session("a", proto="tcp") is first
        assert stage.sessions() == (first,)
        handle = first.participant("10.8.0.2:44300", isn=1000)
        assert first.participant("10.8.0.2:44300") == handle
        assert first.participant("10.8.0.2:44300", isn=1000) == handle
        with pytest.raises(ValueError, match="origin cannot move"):
            first.participant("10.8.0.2:44300", isn=2000)
        with pytest.raises(ValueError, match="created with proto 'tcp'"):
            stage.session("a", proto="udp")


def test_an_ended_session_takes_no_more_records():
    with zpf.sessionize_stage(
        io.BytesIO(unit_sequence_of_packets()), io.BytesIO(),
        decoder="tcp-reassembly", produced_by="t", produced_at=2,
    ) as stage:
        (stream,) = stage.streams()
        flow = stage.session("a", proto="tcp")
        sender = flow.participant("10.8.0.2:44300", isn=1000)
        flow.record(sender, b"A" * 40, cites=stream.cite(0, 40), hints=zpf.Hints(seq_start=1001))
        flow.end("fin")
        assert flow.ended
        with pytest.raises(zpf.ZpfError, match="has ended"):
            flow.record(sender, b"B", cites=stream.cite(60, 61), hints=zpf.Hints(seq_start=1041))


def test_the_decoder_is_always_declared_at_the_transport_layer():
    sink = io.BytesIO()
    with zpf.sessionize_stage(
        io.BytesIO(unit_sequence_of_packets()), sink,
        decoder=("tcp-reassembly", "2"), produced_by="t", produced_at=2,
        params_digest="sha256:aa",
    ):
        pass
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        (decoder,) = out.decoders.values()
        assert decoder.output_layer == zpf.OutputLayer.TRANSPORT
        assert (decoder.name, decoder.version, decoder.params_digest) == (
            "tcp-reassembly", "2", "sha256:aa",
        )


# --- Inner pure ACKs (zipline#160, 0.23) ------------------------------------------


def packet_stream(*sizes: int) -> bytes:
    """Build a packet stream of the given sizes; the test knows what each one is."""
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1_000_000, produced_by="t", produced_at=1) as writer:
        outer = writer.add_source("zpf-input", uri="outer.zpf", digest="sha256:00")
        decrypt = writer.add_decoder("wireguard-decrypt", output_layer=zpf.OutputLayer.DECODED)
        session = writer.begin_session(proto="ip", session_id=5)
        tunnel = session.participant("10.8.0.2", adjacency=zpf.Adjacency.UNITS)
        for k, size in enumerate(sizes):
            session.record(
                tunnel, ts=1000 + 100 * k, payload=bytes([0x45]) * size,
                decoded=zpf.Decoded(
                    decoder=decrypt, content_type=zpf.NET_IP,
                    spans=[zpf.Span(source_id=outer.source_id, session_id=1,
                                    participant_id=0, off_start=80 * k, off_end=80 * k + 80)],
                ),
            )
    return sink.getvalue()


@pytest.mark.parametrize("sequenced", [True, False])
def test_an_inner_pure_ack_is_a_zero_length_record_citing_its_packet(sequenced: bool):
    """Every input packet is covered, and the flow still orders.

    A pure-ACK packet carries no payload, but its bytes in the input still owe
    coverage, and its ack is ordering information. A zero-length record citing
    the packet keeps both. Through 0.22 that record ended at its offset, so
    the handshake, data, pure-ACK flow below was refused when sequenced and
    stalled when merged (zipline#160).
    """
    # SYN, SYN-ACK, 8 bytes of client data, and the server's pure ACK, as
    # 40-, 40-, 48- and 40-byte packets.
    raw = packet_stream(40, 40, 48, 40)
    sink = io.BytesIO()
    with zpf.sessionize_stage(
        io.BytesIO(raw), sink, decoder="tcp-reassembly", produced_by="t", produced_at=2
    ) as stage:
        (stream,) = stage.streams()
        packets = [(d.off_start, d.off_end) for d in stream.datagrams()]
        flow = stage.session("c -> s", proto="tcp", sequenced=sequenced)
        client = flow.participant("10.0.0.1:51000", isn=1000)
        server = flow.participant("10.0.0.2:80", isn=5000)
        syn = zpf.RecordFlags.SYN
        for sender, payload, seq, ack, flags, packet in (
            (client, b"", 1001, None, syn, packets[0]),
            (server, b"", 5001, 1001, syn, packets[1]),
            (client, b"GET /\r\n\r", 1001, 5001, zpf.RecordFlags.PSH, packets[2]),
            (server, b"", 5001, 1009, 0, packets[3]),  # the pure ACK
        ):
            flow.record(sender, payload, cites=stream.cite(*packet),
                        hints=zpf.Hints(seq_start=seq, ack=ack), flags=flags)

    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert out.diagnostics == []
        (session,) = out.sessions()
        assert session.sequenced is sequenced
        if sequenced:
            session.verify()
        assert [r.timestamp for r in session.timeline()] == [1000, 1100, 1200, 1300]
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []


# --- ts_first (#86) ---------------------------------------------------------------


def _coalesce(ts: int | None = None, ts_first: int | None = None) -> zpf.Record:
    """Write one flow record coalescing the first and third packets; return it."""
    raw = unit_sequence_of_packets()  # packets at ts 1000, 1100, 1200
    sink = io.BytesIO()
    with zpf.sessionize_stage(
        io.BytesIO(raw), sink, decoder="tcp-reassembly", produced_by="t", produced_at=2
    ) as stage:
        (stream,) = stage.streams()
        flow = stage.session("a", proto="tcp")
        flow.record(
            flow.participant("10.8.0.2:44300", isn=1000), b"A" * 40 + b"B" * 30,
            cites=[stream.cite(0, 40), stream.cite(60, 90)],
            hints=zpf.Hints(seq_start=1001), ts=ts, ts_first=ts_first,
        )
        flow_b = stage.session("b", proto="tcp")
        flow_b.record(flow_b.participant("10.8.0.2:44301", isn=5000), b"C" * 20,
                      cites=stream.cite(40, 60), hints=zpf.Hints(seq_start=5001))
    assert zpf.check_coverage(io.BytesIO(sink.getvalue()), io.BytesIO(raw)) == []
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        return next(iter(out.session(0).records()))


def test_a_coalesced_record_derives_ts_first_with_ts():
    record = _coalesce()
    assert (record.ts_first, record.timestamp) == (1000, 1200)


def test_an_explicit_ts_first_is_written_as_given():
    assert _coalesce(ts_first=990).ts_first == 990
    assert _coalesce(ts=1500, ts_first=990).ts_first == 990


def test_an_explicit_ts_leaves_ts_first_to_the_caller():
    # Deriving one half of the pair against the caller's other half could
    # write a ts_first later than ts.
    record = _coalesce(ts=1500)
    assert (record.timestamp, record.ts_first) == (1500, None)


def test_a_record_from_one_packet_carries_no_ts_first():
    sink = io.BytesIO()
    sessionize_tunnel(sink, per_flow=True)
    with zpf.open(io.BytesIO(sink.getvalue())) as out:
        assert all(r.ts_first is None for s in out.sessions() for r in s.records())

"""Decoder tutorial stage 2: write a decode-stage file.

Reads ``rest_raw.zpf`` (run ``05_rest_raw_input.py`` first, same directory),
reassembles each participant's byte stream, splits it into HTTP messages,
and writes ``rest_decoded.zpf`` -- a *decode-stage* file whose records are
whole HTTP messages instead of transport chunks. Each decoded record cites
the exact input bytes it came from, and the coverage check at the end
confirms nothing was dropped.
"""

import hashlib
from pathlib import Path

import zpf

# --- a deliberately tiny, illustrative HTTP splitter --------------------
# Real HTTP has chunked transfer-encoding, pipelining subtleties, and more;
# this handles only the fixed-length case the sample traffic uses.


def content_length(headers: bytes) -> int:
    """Return the ``Content-Length`` header value, or 0 if absent."""
    for line in headers.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value.strip())
    return 0


def decode_stream(stream: bytes) -> tuple[list[tuple[int, int, str]], int]:
    """Split a reassembled stream into HTTP messages.

    Returns the ``(start, end, kind)`` of each complete message and the
    offset up to which parsing succeeded; ``[consumed, len(stream))`` is
    whatever could not be decoded.
    """
    messages: list[tuple[int, int, str]] = []
    pos = 0
    while pos < len(stream):
        header_end = stream.find(b"\r\n\r\n", pos)
        if header_end == -1:
            break  # headers not terminated -> stop here
        header_end += 4
        end = header_end + content_length(stream[pos:header_end])
        if end > len(stream):
            break  # body not fully present yet
        kind = "response" if stream[pos:].startswith(b"HTTP/") else "request"
        messages.append((pos, end, kind))
        pos = end
    return messages, pos


# --- the decoder: rest_raw.zpf -> rest_decoded.zpf ----------------------

RAW = "rest_raw.zpf"
digest = "sha256:" + hashlib.sha256(Path(RAW).read_bytes()).hexdigest()

with zpf.open(RAW) as reader:
    (raw_session,) = reader.sessions()
    input_id = raw_session.session_id

    with zpf.create(
        "rest_decoded.zpf",
        tick_hz=reader.header.tick_hz,
        produced_by="http-decode 1.0",
        produced_at=1_700_000_000,
    ) as writer:
        source = writer.add_source("zpf-input", uri=RAW, digest=digest)
        http = writer.add_decoder("http/1.1", version="tutorial")

        with writer.begin_session(proto="http", key=raw_session.key, session_id=input_id) as out:
            # Re-declare the participants (same ids as the input, for clarity).
            handles = {
                p.participant_id: out.participant(p.endpoint) for p in raw_session.participants
            }

            for participant in raw_session.participants:
                pid = participant.participant_id
                origin = participant.isn + 1
                records = list(raw_session.stream(pid))
                stream = b"".join(r.payload for r in records)
                base = records[0].seq_start - origin  # logical offset of stream[0]

                messages, consumed = decode_stream(stream)
                for start, end, kind in messages:
                    out.record(
                        handles[pid],
                        ts=records[0].timestamp,
                        payload=stream[start:end],
                        decoder=http,
                        content_type=f"dec:http-{kind}",
                        spans=(
                            zpf.Span(
                                source_id=source.source_id,
                                session_id=input_id,
                                participant_id=pid,
                                off_start=base + start,
                                off_end=base + end,
                            ),
                        ),
                    )
                if consumed < len(stream):  # nothing left undecoded here, but be honest
                    writer.undecoded(
                        source,
                        input_id,
                        pid,
                        base + consumed,
                        base + len(stream),
                        reason="truncated",
                        decoder=http,
                    )

# Read the decode stage back, then prove it accounts for every input byte.
with zpf.open("rest_decoded.zpf") as decoded:
    print(f"file_kind: {decoded.file_kind}")
    print(f"decoder:   {decoded.decoders[0].name} {decoded.decoders[0].version}")
    (session,) = decoded.sessions()
    for record in session.records():
        print(f"  {record.content_type}: {record.payload[:40]!r}")

findings = zpf.check_coverage("rest_decoded.zpf", RAW)
print(f"check_coverage: {findings}")

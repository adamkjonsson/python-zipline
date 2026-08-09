"""Decoder tutorial stage 2: write a decode-stage file.

Reads ``rest_raw.zpf`` (run ``05_rest_raw_input.py`` first, same directory),
reassembles each participant's byte stream, splits it into HTTP messages,
and writes ``rest_decoded.zpf`` -- a *decode-stage* file whose records are
whole HTTP messages instead of transport chunks. Each decoded record cites
the exact input bytes it came from, and the coverage check at the end
confirms nothing was dropped.
"""

from datetime import UTC, datetime

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
# `decode_stage` opens the input, copies its time base, declares it as a
# zpf-input source (hashing it for the digest), re-declares the participants
# under the same ids, and hands back one context per input stream.

RAW = "rest_raw.zpf"

with zpf.decode_stage(
    RAW,
    "rest_decoded.zpf",
    decoder=("http/1.1", "tutorial"),  # name, version
    produced_by="http-decode 1.0",
    produced_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
    proto="http",
) as dec:
    for stream in dec.streams():
        for segment in stream.segments():  # contiguous reassembled bytes
            messages, _consumed = decode_stream(segment.data)
            for start, end, kind in messages:
                # cites=(...) mints the span for us: the input's ids are filled
                # in, and offsets are relative to the whole stream.
                dec.record(
                    stream,
                    segment.data[start:end],
                    ts=segment.ts,
                    content_type=f"dec:http-{kind}",
                    cites=(segment.off_start + start, segment.off_start + end),
                )
# On exit decode_stage marks any bytes we did not cite as Undecoded, so the
# coverage guarantee below holds without a single undecoded() call by hand.

# Read the decode stage back, then prove it accounts for every input byte.
with zpf.open("rest_decoded.zpf") as decoded:
    provenance, layer = decoded.stream_kind(7, 0)
    print(f"stream 0:  {provenance.name.lower()} {layer.name.lower()}")
    print(f"decoder:   {decoded.decoders[0].name} {decoded.decoders[0].version}")
    (session,) = decoded.sessions()
    for record in session.records():
        print(f"  {record.content_type}: {record.payload[:40]!r}")

findings = zpf.check_coverage("rest_decoded.zpf", RAW)
print(f"check_coverage: {findings}")

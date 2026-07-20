"""Decoder tutorial stage 3: undecoded regions and the coverage guarantee.

A decoder must account for *every* input byte: each one is either covered
by a decoded record's span or marked Undecoded -- never silently dropped.
This script decodes a stream whose tail is a truncated, unparseable request,
marks that tail Undecoded, and uses ``zpf.check_coverage`` to prove the
guarantee holds -- then shows the gap that check reports if the marker is
left out.
"""

import hashlib
from pathlib import Path

import zpf

CLIENT_ISN = 1000
COMPLETE = b"GET /users/1 HTTP/1.1\r\nHost: api.example.com\r\n\r\n"
TRUNCATED = b"GET /users/2 HTTP/1.1\r\nHost: "  # no blank line: never finished
STREAM = COMPLETE + TRUNCATED


def write_raw(path: str) -> None:
    """Write a raw file whose client stream ends in a truncated request."""
    with zpf.create(path, tick_hz=1_000_000) as writer:
        writer.add_source("capture", uri="rest.pcap")
        with writer.begin_session(proto="tcp", session_id=7) as session:
            client = session.participant("10.0.0.1:51000", isn=CLIENT_ISN)
            session.record(client, ts=1, payload=STREAM, seq_start=CLIENT_ISN + 1)


def write_decoded(path: str, raw: str, *, mark_undecoded: bool) -> None:
    """Decode the one complete request; optionally mark the truncated tail."""
    digest = "sha256:" + hashlib.sha256(Path(raw).read_bytes()).hexdigest()
    boundary = STREAM.find(b"\r\n\r\n") + 4  # end of the one complete request
    with zpf.create(
        path, tick_hz=1_000_000, produced_by="http-decode 1.0", produced_at=1_700_000_000
    ) as writer:
        source = writer.add_source("zpf-input", uri=raw, digest=digest)
        http = writer.add_decoder("http/1.1")
        with writer.begin_session(proto="http", session_id=7) as out:
            client = out.participant("10.0.0.1:51000")
            out.record(
                client,
                ts=1,
                payload=STREAM[:boundary],
                decoder=http,
                content_type="dec:http-request",
                spans=(
                    zpf.Span(
                        source_id=source.source_id,
                        session_id=7,
                        participant_id=0,
                        off_start=0,
                        off_end=boundary,
                    ),
                ),
            )
            if mark_undecoded:
                writer.undecoded(
                    source, 7, 0, boundary, len(STREAM), reason="truncated", decoder=http
                )


write_raw("cov_raw.zpf")

write_decoded("cov_decoded.zpf", "cov_raw.zpf", mark_undecoded=True)
print(f"with the Undecoded marker: {zpf.check_coverage('cov_decoded.zpf', 'cov_raw.zpf')}")

write_decoded("cov_gap.zpf", "cov_raw.zpf", mark_undecoded=False)
for finding in zpf.check_coverage("cov_gap.zpf", "cov_raw.zpf"):
    print(f"without it: {finding.category}: {finding.message}")

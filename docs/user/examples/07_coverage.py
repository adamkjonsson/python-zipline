"""Decoder tutorial stage 3: undecoded regions and the coverage guarantee.

A decoder must account for *every* input byte: each one is either covered
by a decoded record's span or marked Undecoded -- never silently dropped.
``decode_stage`` closes that loop for you: on exit it marks whatever you
left uncited as Undecoded, so the guarantee holds by construction. This
script decodes a stream whose tail is a truncated, unparseable request,
lets auto-fill mark the tail, and uses ``zpf.check_coverage`` to prove the
guarantee holds -- then shows the ``coverage-gap`` that check reports when
auto-fill is turned off and the tail is left unmarked.
"""

from datetime import UTC, datetime

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


def decode(path: str, raw: str, *, fill_undecoded: bool) -> None:
    """Decode the one complete request, leaving the truncated tail alone."""
    boundary = STREAM.find(b"\r\n\r\n") + 4  # end of the one complete request
    with zpf.decode_stage(
        raw,
        path,
        decoder="http/1.1",
        produced_by="http-decode 1.0",
        produced_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
        proto="http",
        fill_undecoded=fill_undecoded,
    ) as dec:
        (client,) = dec.streams()
        dec.record(
            client,
            STREAM[:boundary],
            ts=1,
            content_type="dec:http-request",
            cites=(0, boundary),
        )


write_raw("cov_raw.zpf")

# Auto-fill on (the default): the untouched tail is marked for us.
decode("cov_decoded.zpf", "cov_raw.zpf", fill_undecoded=True)
print(f"auto-fill on:  {zpf.check_coverage('cov_decoded.zpf', 'cov_raw.zpf')}")
with zpf.open("cov_decoded.zpf") as decoded:
    (marker,) = decoded.undecoded
    print(f"auto-filled reason={marker.reason!r} over [{marker.off_start}, {marker.off_end})")

# Auto-fill off, and we never mark the tail: check_coverage catches the hole.
decode("cov_gap.zpf", "cov_raw.zpf", fill_undecoded=False)
for finding in zpf.check_coverage("cov_gap.zpf", "cov_raw.zpf"):
    print(f"auto-fill off: {finding.category}: {finding.message}")

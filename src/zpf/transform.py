"""File → file transforms: the merge, and the coverage-guarantee validator.

All derivation in ZPF is file → file. This module implements the two
transform-side tools the specification describes:

* :func:`merge_files` — the **merge transform**: two separately-captured
  directions of one conversation (``sideA.zpf`` + ``sideB.zpf``) are
  combined into a single **sequenced pass-through** file. The streaming
  causal merge runs once, here, so every downstream reader consumes the
  session in stored order and never pays for ordering again.
* :func:`check_coverage` — the decode-stage **coverage guarantee**
  validator: in a decoder's output, every region of every input
  participant stream must be either covered by a decoded record's
  ``spans`` or marked Undecoded — nothing silently dropped, and never
  both.

This version of the merge handles the canonical two-tap case exactly:
two raw inputs, each holding one session with one participant (one
captured direction). Anything else is reported as an error rather than
guessed at.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING

from zpf._intervals import complement, intersections
from zpf.blocks import Origin, SourceKind
from zpf.errors import Diagnostic, ZpfError
from zpf.order import SEQ_SPACE, causal_merge, record_end
from zpf.reader import FileReader, SessionReader
from zpf.writer import FileWriter, ParticipantHandle, SessionWriter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from zpf.blocks import Record

# One input participant stream, and its cited [start, end) offset ranges.
_StreamKey = tuple[int, int]  # (session_id, pid), in the input's namespace
_Intervals = dict[_StreamKey, list[tuple[int, int]]]


def merge_files(
    side_a: str | os.PathLike[str] | IO[bytes] | IO[str],
    side_b: str | os.PathLike[str] | IO[bytes] | IO[str],
    output: str | os.PathLike[str] | IO[bytes],
    *,
    produced_by: str,
    produced_at: int | None = None,
    creator: str | None = None,
) -> None:
    """Merge two single-direction raw captures into one sequenced file.

    The output is a **pass-through** derived file per the specification:
    each input is declared as a ``zpf-input`` Source (with a sha256
    ``digest`` when the input is a path), the output session carries the
    SEQUENCED flag, every participant maps back to its input stream via
    ``origin``, and records are re-emitted byte-identically — payloads,
    timestamps, ``seq_start``/``ack`` hints, flags, and unknown options
    preserved; ``spans`` stripped (``origin`` plus preserved offsets are a
    pass-through file's provenance).

    Args:
        side_a: First input — a raw file holding one session with one
            participant (one captured direction).
        side_b: Second input, the other direction.
        output: Where to write the merged binary file.
        produced_by: Tool + version running the transform (File Header
            provenance, required for derived files).
        produced_at: Wall-clock build time in Unix seconds; defaults to
            now.
        creator: Optional File Header ``creator``.

    Raises:
        ZpfError: If the inputs are not the canonical mergeable shape
            (not raw, more than one session or participant, mismatched
            clocks).
        SemanticError: If the inputs' ordering hints are inconsistent
            (the causal merge stalls).

    """
    with FileReader(side_a) as reader_a, FileReader(side_b) as reader_b:
        _require_mergeable(reader_a, reader_b)
        session_a = reader_a.sessions()[0]
        session_b = reader_b.sessions()[0]
        header = reader_a.header
        writer = FileWriter(
            output,
            tick_hz=header.tick_hz,
            time_epoch=header.time_epoch,
            creator=creator,
            produced_by=produced_by,
            produced_at=int(time.time()) if produced_at is None else produced_at,
        )
        with writer:
            source_a = writer.add_source(
                "zpf-input", uri=_uri_of(side_a), digest=_digest_of(side_a)
            )
            source_b = writer.add_source(
                "zpf-input", uri=_uri_of(side_b), digest=_digest_of(side_b)
            )
            session = writer.begin_session(
                proto=session_a.proto, key=session_a.key, sequenced=True
            )
            out_a = _copy_participant(session, session_a, source_a.source_id)
            out_b = _copy_participant(session, session_b, source_b.source_id)
            streams = {
                out_a.pid: _reissued(session_a, session.session_id, out_a.pid,
                                     source_a.source_id),
                out_b.pid: _reissued(session_b, session.session_id, out_b.pid,
                                     source_b.source_id),
            }
            for record in causal_merge(streams):
                writer.write_block(record)
            session.end()


def _require_mergeable(reader_a: FileReader, reader_b: FileReader) -> None:
    """Check both inputs have the canonical one-direction shape."""
    for name, reader in (("side_a", reader_a), ("side_b", reader_b)):
        if reader.file_kind not in (None, "raw"):
            msg = f"{name} is a {reader.file_kind} file; the merge takes raw captures"
            raise ZpfError(msg)
        sessions = reader.sessions()
        if len(sessions) != 1:
            msg = f"{name} holds {len(sessions)} sessions; the merge takes exactly one"
            raise ZpfError(msg)
        if len(sessions[0].participants) != 1:
            msg = (
                f"{name} session {sessions[0].session_id} has "
                f"{len(sessions[0].participants)} participants; the merge takes one "
                "captured direction per input"
            )
            raise ZpfError(msg)
    header_a, header_b = reader_a.header, reader_b.header
    if header_a.tick_hz != header_b.tick_hz:
        msg = f"tick_hz differs: {header_a.tick_hz} vs {header_b.tick_hz}"
        raise ZpfError(msg)
    if (header_a.time_epoch or 0) != (header_b.time_epoch or 0):
        msg = f"time_epoch differs: {header_a.time_epoch} vs {header_b.time_epoch}"
        raise ZpfError(msg)


def _copy_participant(
    session: SessionWriter, input_session: SessionReader, source_id: int
) -> ParticipantHandle:
    """Re-declare an input's participant in the output, with its origin."""
    participant = input_session.participants[0]
    return session.participant(
        participant.endpoints,
        isn=participant.isn,
        tcp_role=participant.tcp_role,
        identity=participant.identity,
        comment=participant.comment,
        origin=Origin(
            source_id=source_id,
            session_id=input_session.session_id,
            participant_id=participant.participant_id,
        ),
    )


def _reissued(
    input_session: SessionReader, session_id: int, pid: int, source_id: int
) -> Iterator[Record]:
    """Yield an input stream's records re-addressed to the output ids."""
    input_pid = input_session.participants[0].participant_id
    for record in input_session.stream(input_pid):
        yield dataclasses.replace(
            record,
            session_id=session_id,
            sender_pid=pid,
            source_id=source_id,
            spans=(),  # pass-through records carry no spans
        )


def _uri_of(source: str | os.PathLike[str] | IO[bytes] | IO[str]) -> str | None:
    if isinstance(source, (str, os.PathLike)):
        return os.fspath(source) if isinstance(source, os.PathLike) else source
    return None


def _digest_of(source: str | os.PathLike[str] | IO[bytes] | IO[str]) -> str | None:
    """Return ``sha256:<hex>`` for a path input; ``None`` for streams."""
    if not isinstance(source, (str, os.PathLike)):
        return None
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    return f"sha256:{digest}"


# --- Coverage validation --------------------------------------------------------


def check_coverage(
    decoded: str | os.PathLike[str] | IO[bytes] | IO[str],
    raw: str | os.PathLike[str] | IO[bytes] | IO[str],
) -> list[Diagnostic]:
    """Validate a decode stage's coverage guarantee against its input.

    Per the specification, within each input participant stream every
    offset must be covered either by some decoded record's ``spans`` or by
    an Undecoded block — never silently dropped, never both.

    Args:
        decoded: The decode-stage output file.
        raw: The predecessor input file the decoded file cites.

    Returns:
        The violations found, empty when the guarantee holds — categories
        ``"coverage-gap"`` (an input range neither decoded nor marked),
        ``"coverage-overlap"`` (a range both decoded and Undecoded), and
        ``"coverage-excess"`` (a cited range beyond the input stream).
        ``Diagnostic.offset`` is the range's first offset.

    Raises:
        ZpfError: If the decoded file does not cite exactly one
            identifiable ``zpf-input`` Source.

    """
    with FileReader(decoded) as decoded_reader, FileReader(raw) as raw_reader:
        source_id = _input_source_id(decoded_reader, raw)
        extents = _stream_extents(raw_reader)
        spans, undecoded = _cited_intervals(decoded_reader, source_id)
        findings: list[Diagnostic] = []
        for key in sorted(set(extents) | set(spans) | set(undecoded)):
            findings.extend(
                _check_stream(key, extents.get(key, 0), spans.get(key, []),
                              undecoded.get(key, []))
            )
        return findings


def _input_source_id(decoded: FileReader, raw: object) -> int:
    """Find the zpf-input Source of ``decoded`` that cites the raw file."""
    inputs = [
        source
        for source in decoded.sources.values()
        if source.kind == SourceKind.ZPF_INPUT
    ]
    if len(inputs) == 1:
        return inputs[0].source_id
    if isinstance(raw, (str, os.PathLike)):
        digest = _digest_of(raw)
        matching = [source for source in inputs if source.digest == digest]
        if len(matching) == 1:
            return matching[0].source_id
    msg = (
        f"the decoded file cites {len(inputs)} zpf-input sources and none is "
        "identifiable as the given raw file (by digest)"
    )
    raise ZpfError(msg)


def _stream_extents(raw: FileReader) -> dict[tuple[int, int], int]:
    """Compute each raw participant stream's logical extent (hole-inclusive)."""
    extents: dict[tuple[int, int], int] = {}
    for session in raw.sessions():
        for participant in session.participants:
            pid = participant.participant_id
            origin = None if participant.isn is None else (participant.isn + 1) % SEQ_SPACE
            hinted_extent = 0
            cumulative = 0
            for record in session.stream(pid):
                if record.seq_start is not None:
                    if origin is None:
                        origin = record.seq_start
                    end = record_end(record.seq_start, len(record.payload))
                    hinted_extent = (end - origin) % SEQ_SPACE
                else:
                    cumulative += len(record.payload)
            extents[(session.session_id, pid)] = (
                hinted_extent if origin is not None else cumulative
            )
    return extents


def _cited_intervals(decoded: FileReader, source_id: int) -> tuple[_Intervals, _Intervals]:
    """Collect decoded-span and Undecoded intervals, keyed by input stream."""
    spans: _Intervals = {}
    undecoded: _Intervals = {}
    for session in decoded.sessions():
        for record in session.records():
            for span in record.spans:
                if span.source_id == source_id and span.off_start < span.off_end:
                    key = (span.session_id, span.participant_id)
                    spans.setdefault(key, []).append((span.off_start, span.off_end))
    for marker in decoded.undecoded:
        if marker.source_id == source_id and marker.off_start < marker.off_end:
            key = (marker.session_id, marker.participant_id)
            undecoded.setdefault(key, []).append((marker.off_start, marker.off_end))
    return spans, undecoded


def _check_stream(
    key: tuple[int, int],
    extent: int,
    spans: list[tuple[int, int]],
    undecoded: list[tuple[int, int]],
) -> list[Diagnostic]:
    """Check one input stream's coverage; return its violations."""
    session_id, pid = key
    where = f"input stream (session {session_id}, pid {pid})"
    findings: list[Diagnostic] = []
    for start, end in intersections(sorted(spans), sorted(undecoded)):
        message = f"{where}: [{start}, {end}) is both decoded and marked Undecoded"
        findings.append(Diagnostic(start, "coverage-overlap", message))
    covered = sorted(spans + undecoded)
    for start, end in complement(covered, extent):
        message = f"{where}: [{start}, {end}) is neither decoded nor marked Undecoded"
        findings.append(Diagnostic(start, "coverage-gap", message))
    for start, end in covered:
        if end > extent:
            message = (
                f"{where}: [{start}, {end}) reaches past the stream's extent ({extent})"
            )
            findings.append(Diagnostic(start, "coverage-excess", message))
    return findings



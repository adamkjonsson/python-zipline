"""File → file transforms: the merge, and the coverage-guarantee validator.

All derivation in ZPF is file → file. This module implements the
transform-side tools the specification describes:

* :func:`merge_files` — the **merge transform**: two separately-captured
  directions of one conversation (``sideA.zpf`` + ``sideB.zpf``) are
  combined into a single **sequenced pass-through** file. The streaming
  causal merge runs once, here, so every downstream reader consumes the
  session in stored order and never pays for ordering again.
* :func:`rewrite_decoded` — a decoded-layer **filter or reordering stage**.
  Both change the offsets stored order defines, so both are decode stages
  rather than pass-throughs, inheriting the input's ``decoder_id`` values
  and marking what they drop.
* :func:`check_coverage` — the decode-stage **coverage guarantee**
  validator: in a decoder's output, every region of every input
  participant stream must be either covered by a decoded record's
  ``spans`` or marked Undecoded — nothing silently dropped, and never
  both.
* :func:`resolve_spans` — the provenance walk: one hop for a record its own
  stage built, two for one a pass-through re-emitted.

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
from zpf.blocks import Origin, SourceKind, Span
from zpf.errors import Diagnostic, ZpfError
from zpf.order import causal_merge
from zpf.reader import FileReader, SessionReader
from zpf.reassembly import stream_extent
from zpf.writer import DecoderHandle, DerivedInput, FileWriter, ParticipantHandle, SessionWriter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from datetime import datetime

    from zpf.blocks import Record, Source

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


def resolve_spans(
    derived: str | os.PathLike[str] | IO[bytes] | IO[str],
    session_id: int,
    pid: int,
    index: int,
    *,
    open_input: Callable[[Source], str | os.PathLike[str] | IO[bytes] | IO[str]] | None = None,
) -> tuple[Span, ...]:
    """Resolve which upstream ranges a record's bytes came from.

    A record's provenance is one hop or two, and which it is depends on
    whether this file's own stage *built* the record:

    * A **decode stage's** record carries ``spans`` naming the input ranges
      it was built from. One hop; those spans are the answer.
    * A **pass-through's** record carries none — ``origin`` plus offset
      preservation is its provenance. So the walk takes the participant's
      ``origin`` to the corresponding stream in the input, computes the
      record's :meth:`~zpf.SessionReader.ranges` position (offsets are
      preserved, so it is the same range there), and reads the ``spans`` of
      the record it finds. That file alone cannot say which raw bytes a
      record came from, which is the asymmetry this function hides.

    Chained pass-throughs recurse, one level at a time.

    Args:
        derived: The file holding the record.
        session_id: Its session id, in *this* file's namespace.
        pid: Its participant id, in this file's namespace.
        index: The record's position in that participant's stored order.
        open_input: How to open a ``zpf-input`` Source this file names.
            Defaults to resolving the Source's ``uri`` beside ``derived``,
            which requires ``derived`` to be a path.

    Returns:
        The spans naming the upstream ranges, empty when the file records
        no provenance for it (a raw file, or a participant with no
        ``origin``). Each span's ids are read in the namespace of *the
        source it names*, as spans always are — which for a two-hop
        resolution is a file further up the chain than ``derived``, not
        ``derived`` itself.

    Raises:
        IndexError: If the participant has no record at ``index``.
        ZpfError: If an input must be opened but no ``open_input`` was
            given and ``derived`` is not a path.

    """
    opener = open_input or _sibling_opener(derived)
    with FileReader(derived) as reader:
        session = reader.session(session_id)
        records = list(session.stream(pid))
        record = records[index]
        if record.spans:
            return record.spans
        origin = session.participant(pid).origin
        if origin is None:
            return ()
        wanted = session.ranges(pid)[index]
        source = reader.sources[origin.source_id]
    return _resolve_at(opener(source), origin, wanted, opener)


def _resolve_at(
    target: str | os.PathLike[str] | IO[bytes] | IO[str],
    origin: Origin,
    wanted: tuple[int, int],
    opener: Callable[[Source], str | os.PathLike[str] | IO[bytes] | IO[str]],
) -> tuple[Span, ...]:
    """Collect the spans covering ``wanted`` in one input stream."""
    found: list[Span] = []
    deeper: list[tuple[Origin, tuple[int, int]]] = []
    with FileReader(target) as reader:
        session = reader.session(origin.session_id)
        pid = origin.participant_id
        inner = session.participant(pid).origin
        for record, (start, end) in zip(
            session.stream(pid), session.ranges(pid), strict=True
        ):
            if end <= wanted[0] or start >= wanted[1]:
                continue
            if record.spans:
                found.extend(record.spans)
            elif inner is not None:
                deeper.append((inner, (start, end)))
        sources = dict(reader.sources)
    for next_origin, next_range in deeper:
        found.extend(
            _resolve_at(opener(sources[next_origin.source_id]), next_origin, next_range, opener)
        )
    return tuple(found)


def _sibling_opener(
    derived: str | os.PathLike[str] | IO[bytes] | IO[str],
) -> Callable[[Source], str | os.PathLike[str] | IO[bytes] | IO[str]]:
    """Resolve an input Source's ``uri`` beside the file that names it."""
    if not isinstance(derived, (str, os.PathLike)):
        msg = "resolving an input needs open_input when the file is a stream, not a path"
        raise ZpfError(msg)
    parent = Path(derived).parent

    def open_input(source: Source) -> str | os.PathLike[str] | IO[bytes] | IO[str]:
        if source.uri is None:
            msg = f"source {source.source_id} names no uri, so it cannot be opened"
            raise ZpfError(msg)
        return parent / source.uri

    return open_input


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
    """Compute each input participant stream's extent, in its own offset space.

    Delegates to the shared rule so the coverage check and the reader
    cannot drift: hole-inclusive for a hinted transport stream, positional
    for a decoded one. That matters for a chained decode
    (``raw -> tls-records -> http``), where the "raw" argument is itself a
    decode stage's output and its offsets are a concatenation of payloads
    rather than true stream positions — and where, since 0.14, it may carry
    Discontinuity blocks whose declared widths are terms in that sum. Hence
    ``stream_blocks``: passing records alone would measure a chained input
    short by every width it declares, which is exactly the drift this
    delegation exists to prevent.
    """
    extents: dict[tuple[int, int], int] = {}
    for session in raw.sessions():
        for participant in session.participants:
            pid = participant.participant_id
            extents[(session.session_id, pid)] = stream_extent(
                participant, list(session.stream_blocks(pid))
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




def rewrite_decoded(
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | FileReader,
    sink: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    keep: Callable[[Record], bool] | None = None,
    reorder: Callable[[Sequence[Record]], Sequence[Record]] | None = None,
    produced_by: str,
    produced_at: int | datetime,
    uri: str | None = None,
    digest: str | None = None,
    comment: str | None = None,
) -> None:
    """Filter and/or reorder a decoded file's records into a new stage.

    Dropping or reordering a decoded record changes that participant's
    offset space, because stored order is what *defines* it. So this is
    **not** a pass-through, however byte-preserving it looks: the output
    cannot claim to have preserved what it just moved. It is a decode stage,
    and it carries a decode stage's obligations —

    * every emitted record cites the input range it came from in ``spans``;
    * every dropped range is marked :class:`~zpf.Undecoded` with
      ``reason="skipped"``, a deliberate decision not to carry data forward,
      so the coverage guarantee holds over the whole input;
    * ``decoder_id`` names a **layer**, not a stage, so the input's are
      inherited and their Decoder Descriptors re-declared. This stage
      declares no decoder of its own — a filtered HTTP message is still an
      HTTP message — and identifies itself through ``produced_by``.

    A reordering stage's spans will *not* ascend with stored order. That is
    expected: coverage depends on which ranges are covered, not on the order
    they appear in.

    Known gap, inherited from the format: a transform that produces records
    without decoding them has no ``params_digest`` to record its own
    configuration in, so the output states *what* it came from but not
    *how*. A merge has the same gap.

    Args:
        source: The decoded input ``.zpf`` — a path, a seekable stream, or
            an already-open :class:`~zpf.FileReader` (left open).
        sink: Where to write the resulting decode-stage file.
        keep: Predicate deciding which input records survive; the default
            keeps every record, for a pure reordering stage.
        reorder: Rearranges one participant's surviving records. Applied
            after ``keep``, per participant, and defaults to leaving stored
            order alone.
        produced_by: Tool + version running the transform.
        produced_at: Build time; Unix seconds or a tz-aware datetime.
        uri: Where the input lives; defaults to the path it was opened from.
        digest: Content hash of the input; SHA-256 of it when omitted.
        comment: Free-text note for the File Header.

    Raises:
        ZpfError: If the input is not a file carrying a decoded layer.

    Example:
        >>> rewrite_decoded(
        ...     "decoded.zpf", "requests.zpf",
        ...     keep=lambda r: r.content_type == "dec:request",
        ...     produced_by="zpf-filter 1.0", produced_at=1719520000,
        ... )

    """
    owns_reader = not isinstance(source, FileReader)
    reader = FileReader(source) if owns_reader else source
    try:
        _require_decoded_input(reader)
        header = reader.header
        writer = FileWriter(
            sink,
            tick_hz=header.tick_hz,
            time_epoch=header.time_epoch,
            produced_by=produced_by,
            produced_at=produced_at,
            comment=comment,
        )
        with writer:
            derived = writer.derive_from(reader, uri=uri, digest=digest)
            decoders = {
                decoder_id: writer.add_decoder(
                    decoder.name or "",
                    version=decoder.version,
                    params_digest=decoder.params_digest,
                    comment=decoder.comment,
                )
                for decoder_id, decoder in reader.decoders.items()
            }
            for session in reader.sessions():
                out = derived.sessions[session.session_id]
                for participant in session.participants:
                    _rewrite_stream(
                        session, participant.participant_id, writer, out, derived,
                        decoders, keep=keep, reorder=reorder,
                    )
                out.end()
    finally:
        if owns_reader:
            reader.close()


def _require_decoded_input(reader: FileReader) -> None:
    """Reject an input that carries no decoded layer to rewrite."""
    if not reader.decoders:
        msg = (
            "rewrite_decoded takes a file carrying a decoded layer; this one "
            "declares no decoders, so there is no layer to inherit"
        )
        raise ZpfError(msg)


def _rewrite_stream(
    session: SessionReader,
    pid: int,
    writer: FileWriter,
    out: SessionWriter,
    derived: DerivedInput,
    decoders: dict[int, DecoderHandle],
    *,
    keep: Callable[[Record], bool] | None,
    reorder: Callable[[Sequence[Record]], Sequence[Record]] | None,
) -> None:
    """Emit one participant's surviving records, and skip-mark the rest."""
    handle = derived.participants[(session.session_id, pid)]
    records = list(session.stream(pid))
    ranges = session.ranges(pid)
    cited = {id(record): span for record, span in zip(records, ranges, strict=True)}
    survivors = [record for record in records if keep is None or keep(record)]
    if reorder is not None:
        survivors = list(reorder(survivors))
    for record in survivors:
        off_start, off_end = cited[id(record)]
        out.record(
            handle,
            ts=record.timestamp,
            payload=record.payload,
            source=derived.source,
            flags=record.flags,
            decoder=None if record.decoder_id is None else decoders[record.decoder_id],
            content_type=record.content_type,
            spans=(
                Span(
                    source_id=derived.source.source_id,
                    session_id=session.session_id,
                    participant_id=pid,
                    off_start=off_start,
                    off_end=off_end,
                ),
            ),
        )
    kept = sorted(cited[id(record)] for record in survivors)
    extent = ranges[-1][1] if ranges else 0
    for start, end in complement(kept, extent):
        writer.undecoded(
            derived.source, session.session_id, pid, start, end, reason="skipped"
        )

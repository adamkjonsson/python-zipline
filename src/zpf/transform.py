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
* :func:`resolve_spans` — the provenance lookup: one hop, always, since
  every ``zpf``-sourced record carries ``spans`` of its own.

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
from zpf.blocks import Discontinuity, InputExtent, OutputLayer, Record, SourceKind, Span
from zpf.conformance import CoverageLedger
from zpf.errors import Diagnostic, ZpfError
from zpf.order import causal_merge
from zpf.reader import FileReader, SessionReader
from zpf.reassembly import layer_name, stream_extent
from zpf.writer import (
    Decoded,
    DecoderHandle,
    DerivedInput,
    FileWriter,
    InputRef,
    ParticipantHandle,
    SessionWriter,
    SourceHandle,
)

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
    transform_params_digest: str | None = None,
    creator: str | None = None,
) -> None:
    """Merge two single-direction raw captures into one sequenced file.

    The output is a **pass-through** derived file per the specification:
    each input is declared as a ``zpf-input`` Source (with a sha256
    ``digest`` when the input is a path), the output session carries the
    SEQUENCED flag, and records are re-emitted byte-identically — payloads,
    timestamps, ``seq_start``/``ack`` hints, flags, and unknown options
    preserved.

    Each record additionally carries an **identity span**, the same range in
    as out, which since `0.19` is how a pass-through states its provenance;
    through `0.18` its participants carried an ``origin`` instead and its
    records carried no spans at all. Citing the inputs makes this file
    answerable for their coverage, so the holes in a hinted input's offset
    space are marked ``gap`` and each input stream's extent is declared on
    Session End.

    Args:
        side_a: First input — a raw file holding one session with one
            participant (one captured direction).
        side_b: Second input, the other direction.
        output: Where to write the merged binary file.
        produced_by: Tool + version running the transform (File Header
            provenance, required for derived files).
        produced_at: Wall-clock build time in Unix seconds; defaults to
            now.
        transform_params_digest: Hash of this merge's configuration. A
            merge decodes nothing, so it has no Decoder to record its
            parameters on; this is the File Header option 0.14 added for
            exactly that case.
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
            transform_params_digest=transform_params_digest,
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
            out_a = _copy_participant(session, session_a)
            out_b = _copy_participant(session, session_b)
            streams = {
                out_a.pid: _reissued(session_a, session.session_id, out_a.pid,
                                     source_a.source_id),
                out_b.pid: _reissued(session_b, session.session_id, out_b.pid,
                                     source_b.source_id),
            }
            for record in causal_merge(streams):
                writer.write_block(record)
            # The output cites both inputs, so it is answerable for them.
            extents = [
                _account_for_input(writer, session_in, handle)
                for session_in, handle in ((session_a, source_a), (session_b, source_b))
            ]
            session.end(input_extents=extents)


def _account_for_input(
    writer: FileWriter, input_session: SessionReader, source: SourceHandle
) -> InputExtent:
    """Mark the input's holes, and measure it, so coverage closes.

    **A consequence of package A that nothing upstream demonstrates.** Since
    ``0.19`` a pass-through writes an identity span per record, so a merge
    *cites* its input streams — and a file that cites a stream is answerable
    for every offset of it under the coverage guarantee. A transport input's
    holes are real ranges of its offset space that no payload covers, so
    without this the merge emits a file its own reader reports as having an
    unaccounted gap. Through ``0.18`` the question did not arise: a
    pass-through cited nothing and owed nothing.

    The holes are marked ``gap`` — the ``hole`` class, meaning no bytes exist
    there to go and fetch. ``skipped`` or ``dropped`` would claim the merge
    withheld something it had, and send a consumer up the chain after bytes
    that were never captured.

    Returns:
        The input stream's extent, for the output's Session End. Declaring it
        is what makes the merge's coverage checkable from the output alone —
        without a declared length, coverage stopping early is indistinguishable
        from a stream that was that short.

    """
    pid = input_session.participants[0].participant_id
    ranges = input_session.ranges(pid)
    extent = max((end for _, end in ranges), default=0)
    for start, end in complement(sorted(ranges), extent):
        writer.undecoded(
            source, input_session.session_id, pid, start, end, reason="gap"
        )
    return InputExtent(
        source_id=source.source_id,
        session_id=input_session.session_id,
        participant_id=pid,
        extent=extent,
    )


def _require_mergeable(reader_a: FileReader, reader_b: FileReader) -> None:
    """Check both inputs have the canonical one-direction shape.

    The bar is on the **layer**, not on how the file was produced. What the
    merge needs is that its inputs' offsets are hole-inclusive true
    positions, so re-emitting their records preserves the space — and that
    is what ``transport`` means. It used to test ``file_kind == "raw"``,
    which was a proxy for the same thing and stopped being one when 0.15
    made the axes independent: a sessionization stage's output is
    ``zpf``-sourced and transport-shaped, and mergeable for exactly the
    reason a capture's reassembled stream is.
    """
    for name, reader in (("side_a", reader_a), ("side_b", reader_b)):
        for session in reader.sessions():
            for participant in session.participants:
                layer = session.layer(participant.participant_id)
                if layer is not OutputLayer.TRANSPORT:
                    msg = (
                        f"{name} session {session.session_id} participant "
                        f"{participant.participant_id} is at the "
                        f"{layer_name(layer)} layer; the merge preserves its inputs' "
                        f"offsets, which only a transport stream's survive"
                    )
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
    session: SessionWriter, input_session: SessionReader
) -> ParticipantHandle:
    """Re-declare an input's participant in the output.

    ``isn`` **is** copied here, unlike in
    :meth:`~zpf.FileWriter.derive_from`. A merge preserves its inputs' offset
    space rather than replacing it, so the output stream is still anchored at
    the same origin and its records still carry the input's ``seq_start``
    values; dropping ``isn`` would leave those unanchored.
    """
    participant = input_session.participants[0]
    return session.participant(
        participant.endpoints,
        isn=participant.isn,
        tcp_role=participant.tcp_role,
        identity=participant.identity,
        comment=participant.comment,
    )


def _reissued(
    input_session: SessionReader,
    session_id: int,
    pid: int,
    source_id: int,
) -> Iterator[Record]:
    """Yield an input stream's records re-addressed to the output ids.

    Each carries an **identity span** — the same range in as out — which
    since ``0.19`` is how a pass-through states its provenance and how a
    reader tells it from a decode stage. Through ``0.18`` these records
    carried no ``spans`` at all and the participant carried an ``origin``
    instead; the option is gone and every ``zpf``-sourced record owes spans.

    The range comes from :meth:`~zpf.SessionReader.ranges`, which is the
    input's own offset space — and the output preserves that space, so the
    same pair is correct on both sides. That is what makes the span an
    *identity* rather than a citation.
    """
    input_pid = input_session.participants[0].participant_id
    ranges = input_session.ranges(input_pid)
    for record, (off_start, off_end) in zip(
        input_session.stream(input_pid), ranges, strict=True
    ):
        yield dataclasses.replace(
            record,
            session_id=session_id,
            sender_pid=pid,
            source_id=source_id,
            spans=(
                Span(
                    source_id=source_id,
                    session_id=input_session.session_id,
                    participant_id=input_pid,
                    off_start=off_start,
                    off_end=off_end,
                ),
            ),
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
        declared = _declared_extents(decoded_reader, source_id)
        findings: list[Diagnostic] = []
        for key in sorted(set(extents) | set(spans) | set(undecoded)):
            findings.extend(
                _check_stream(key, extents.get(key, 0), spans.get(key, []),
                              undecoded.get(key, []))
            )
        findings.extend(_check_declared(declared, extents))
        return findings


def _declared_extents(decoded: FileReader, source_id: int) -> dict[tuple[int, int], set[int]]:
    """Collect each input stream's declared extents, keyed as the intervals are."""
    declared: dict[tuple[int, int], set[int]] = {}
    for session in decoded.sessions():
        end = session.end
        if end is None:
            continue
        for extent in end.input_extents:
            if extent.source_id == source_id:
                key = (extent.session_id, extent.participant_id)
                declared.setdefault(key, set()).add(extent.extent)
    return declared


def _check_declared(
    declared: dict[tuple[int, int], set[int]], measured: dict[tuple[int, int], int]
) -> list[Diagnostic]:
    """Cross-check declared extents against the input actually opened.

    The check only this function can make: :func:`check_extents` compares a
    declaration against what the file itself accounts for, which catches a
    declaration that contradicts its own spans — but a declaration can be
    self-consistent and still be wrong about the input. With the input in
    hand there is a real length to compare against.
    """
    findings: list[Diagnostic] = []
    for key in sorted(declared):
        session_id, pid = key
        where = f"input stream (session {session_id}, pid {pid})"
        if key not in measured:
            continue
        actual = measured[key]
        for value in sorted(declared[key]):
            if value != actual:
                findings.append(
                    Diagnostic(
                        value,
                        "extent-mismatch",
                        f"{where}: declared extent {value}, but the input measures {actual}",
                    )
                )
    return findings


def check_extents(
    derived: str | os.PathLike[str] | IO[bytes] | IO[str],
) -> list[Diagnostic]:
    """Validate a derived file's coverage claims **from that file alone**.

    The weaker half of :func:`check_coverage`, and the one a consumer can
    always run: it needs no input file, only what this one says about the
    streams it cites. That is what ``input_extents`` is for — without a
    declared length, coverage stopping early is indistinguishable from a
    stream that was that short.

    Args:
        derived: A decode-stage or pass-through file.

    Returns:
        The violations found, empty when the file is self-consistent —
        categories ``"extents-disagree"`` (two sessions declaring different
        lengths for one input stream), ``"extent-exceeds-coverage"`` (a
        declared length beyond what spans plus Undecoded account for: the
        silent-truncation case), ``"extent-below-coverage"`` (a declared
        length the file's own citations overshoot), and ``"coverage-gap"``
        (an interior range neither decoded nor marked, which needs no
        declaration to detect).

    Note:
        ``coverage-gap`` is reported for a **decode stage** only. A
        pass-through re-emits records rather than citing them, so it has no
        ``spans`` and the guarantee is not its to keep — see
        :class:`~zpf.conformance.CoverageLedger`.

    Example:
        >>> for finding in zpf.check_extents("http.zpf"):
        ...     print(finding.category, finding.message)

    """
    ledger = CoverageLedger()
    with FileReader(derived) as reader:
        for block in reader.blocks():
            ledger.observe(block)
    # No file-wide kind to pass any more: the ledger decides per input
    # stream whether this file is answerable for it, by whether any record's
    # spans cited it.
    return [
        Diagnostic(offset, category, message)
        for offset, category, message in ledger.findings()
    ]


def resolve_spans(
    derived: str | os.PathLike[str] | IO[bytes] | IO[str],
    session_id: int,
    pid: int,
    index: int,
    *,
    open_input: Callable[[Source], str | os.PathLike[str] | IO[bytes] | IO[str]] | None = None,
    hops: int | None = 1,
) -> tuple[Span, ...]:
    """Resolve which upstream ranges a record's bytes came from.

    **One hop answers for the immediate input, and since `0.19` every
    ``zpf``-sourced record can.** Its ``spans`` name the ranges it corresponds
    to in the file it was derived from — a decode stage's naming what its unit
    was built from, a pass-through's an **identity span**, the same range in as
    out. Which kind of stage wrote the record does not change the lookup, which
    is what package A bought: through `0.18` a pass-through's records carried no
    spans at all, and answering for one meant opening its input.

    **More hops follow the chain toward the capture.** Each level resolves the
    ranges from the level before, so ``hops=2`` over ``chain/annotated.zpf``
    returns spans naming ``raw.zpf`` rather than ``decoded.zpf``, and
    ``hops=None`` walks until the records answering are capture-sourced and
    have no spans of their own. A span that cannot go further is returned as it
    stands, that being as deep as the chain records.

    Args:
        derived: The file holding the record.
        session_id: Its session id, in *this* file's namespace.
        pid: Its participant id, in this file's namespace.
        index: The record's position in that participant's stored order.
        open_input: How to open a ``zpf-input`` Source. Needed only when
            ``hops`` asks for more than one, since one hop opens nothing.
            Defaults to resolving the Source's ``uri`` beside ``derived``,
            which requires ``derived`` to be a path.
        hops: How far to follow the chain. ``1`` (the default) is the record's
            own spans, which is what the function's name promises; ``None``
            walks to the capture. Must be at least 1.

    Returns:
        The spans naming the upstream ranges, empty when the file records no
        provenance for the record — a capture-sourced one, whose ``source_id``
        is the whole of its provenance. Each span's ids are read in the
        namespace of *the source it names*, as spans always are, so a
        multi-hop result is read against a file further up the chain than
        ``derived``.

    Raises:
        IndexError: If the participant has no record at ``index``.
        ZpfError: If ``hops`` is less than 1, or if a hop must open an input
            and ``derived`` is not a path with no ``open_input`` given.

    Example:
        >>> zpf.resolve_spans("annotated.zpf", 7, 0, 0)             # its input
        >>> zpf.resolve_spans("annotated.zpf", 7, 0, 0, hops=None)  # the capture

    """
    if hops is not None and hops < 1:
        msg = f"hops must be at least 1 (the record's own spans), got {hops}"
        raise ZpfError(msg)
    with FileReader(derived) as reader:
        session = reader.session(session_id)
        records = list(session.stream(pid))
        spans = records[index].spans
        sources = dict(reader.sources)
    if hops is not None and hops == 1:
        # Nothing is opened, so nothing is needed to open it with. Asking for
        # an opener here is what made `open_input` look load-bearing when it
        # was not.
        return spans
    opener = open_input or _sibling_opener(derived)
    remaining = None if hops is None else hops - 1
    found: list[Span] = []
    for span in spans:
        found.extend(_follow(span, sources, opener, remaining))
    return tuple(found)


def _follow(
    span: Span,
    sources: dict[int, Source],
    opener: Callable[[Source], str | os.PathLike[str] | IO[bytes] | IO[str]],
    remaining: int | None,
) -> tuple[Span, ...]:
    """Resolve one span in the file it names, recursing while hops remain.

    Args:
        span: The range to resolve, in the namespace of the source it names.
        sources: The declared Sources of the file the span was read from.
        opener: How to open a ``zpf-input`` Source.
        remaining: Hops left, or ``None`` for as far as the chain goes.

    Returns:
        What the span resolves to, or the span itself where it can go no
        further — a capture-sourced source, or records that carry no spans.

    """
    source = sources.get(span.source_id)
    if source is None or source.kind != SourceKind.ZPF_INPUT:
        return (span,)
    return _resolve_at(opener(source), span, opener, remaining)


def _resolve_at(
    target: str | os.PathLike[str] | IO[bytes] | IO[str],
    span: Span,
    opener: Callable[[Source], str | os.PathLike[str] | IO[bytes] | IO[str]],
    remaining: int | None,
) -> tuple[Span, ...]:
    """Collect what ``span`` corresponds to inside the file it names."""
    with FileReader(target) as reader:
        session = reader.session(span.session_id)
        pid = span.participant_id
        found: list[Span] = []
        for record, (start, end) in zip(
            session.stream(pid), session.ranges(pid), strict=True
        ):
            if end <= span.off_start or start >= span.off_end:
                continue
            found.extend(record.spans)
        sources = dict(reader.sources)
    if not found:
        # The records answering here carry no spans of their own, so this is
        # the deepest the chain records and the span we came with is the answer.
        return (span,)
    if remaining is not None and remaining <= 1:
        return tuple(found)
    deeper: list[Span] = []
    for inner in found:
        deeper.extend(
            _follow(inner, sources, opener, None if remaining is None else remaining - 1)
        )
    return tuple(deeper)


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


def _break_positions(stage1: FileReader) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """Locate every declared break in stage 1's **own output** offset space.

    Returns:
        Per ``(session_id, pid)``, each break as ``(position, width)`` — the
        position being where it sits once the preceding payloads and widths
        are summed, and the width 0 when undeclared.

    """
    breaks: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for session in stage1.sessions():
        for participant in session.participants:
            pid = participant.participant_id
            cursor = 0
            found: list[tuple[int, int]] = []
            for block in session.stream_blocks(pid):
                if isinstance(block, Discontinuity):
                    found.append((cursor, block.width or 0))
                    cursor += block.width or 0
                else:
                    cursor += len(block.payload)
            if found:
                breaks[(session.session_id, pid)] = found
    return breaks


def check_splice(
    stage2: str | os.PathLike[str] | IO[bytes] | IO[str],
    stage1: str | os.PathLike[str] | IO[bytes] | IO[str],
) -> list[Diagnostic]:
    """Check that a stage does not splice across its input's declared breaks.

    The duty is a property of a **pair** of files, which is why it needs its
    own function: stage 1 declares the break, stage 2's ``spans`` cross it,
    and *neither file is wrong on its own*. A harness that checks files
    individually passes both.

    Per the specification, a decode stage reading an input carrying a
    :class:`~zpf.blocks.Discontinuity` MUST NOT emit a unit whose ``spans``
    cross it without emitting a Discontinuity of its own in the
    corresponding position of its output. For a single unit straddling the
    break that position falls *inside* the unit, so the escape is
    unavailable — a stage that cannot express the break "has to leave the
    crossing undone". Hence the check: a stage-2 record violates the rule
    exactly when the ranges it cites in stage 1's output have bytes on both
    sides of a break.

    Args:
        stage2: The downstream file, whose spans cite ``stage1``.
        stage1: The file it decodes, whose breaks are at issue.

    Returns:
        The violations found, empty when none — category
        ``"discontinuity-splice"``. ``Diagnostic.offset`` is the break's
        position in stage 1's output space.

    Raises:
        ZpfError: If ``stage2`` does not cite exactly one identifiable
            ``zpf-input`` Source.

    Example:
        >>> zpf.check_splice("http.zpf", "tls-records.zpf")

    """
    with FileReader(stage2) as downstream, FileReader(stage1) as upstream:
        source_id = _input_source_id(downstream, stage1)
        breaks = _break_positions(upstream)
        if not breaks:
            return []
        findings: list[Diagnostic] = []
        for session in downstream.sessions():
            for index, record in enumerate(session.records()):
                findings.extend(_crossings(record, index, source_id, breaks))
        return findings


def _crossings(
    record: Record,
    index: int,
    source_id: int,
    breaks: dict[tuple[int, int], list[tuple[int, int]]],
) -> list[Diagnostic]:
    """Report each input break this one record's cited ranges straddle."""
    cited: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for span in record.spans:
        if span.source_id == source_id and span.off_start < span.off_end:
            cited.setdefault((span.session_id, span.participant_id), []).append(
                (span.off_start, span.off_end)
            )
    findings: list[Diagnostic] = []
    for key, ranges in sorted(cited.items()):
        session_id, pid = key
        low = min(start for start, _ in ranges)
        high = max(end for _, end in ranges)
        for position, width in breaks.get(key, []):
            if low < position and high > position + width:
                findings.append(
                    Diagnostic(
                        position,
                        "discontinuity-splice",
                        f"record {index} of session {record.session_id} spans "
                        f"[{low}, {high}) of input stream (session {session_id}, pid {pid}), "
                        f"crossing the Discontinuity it declares at {position} — the two "
                        "sides do not join, and this unit welds them",
                    )
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
                participant, list(session.stream_blocks(pid)), session.layer(pid)
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
    transform_params_digest: str | None = None,
    input_ref: InputRef | None = None,
    comment: str | None = None,
) -> None:
    """Filter and/or reorder a decoded file's records into a new stage.

    Dropping or reordering a decoded record changes that participant's
    offset space, because stored order is what *defines* it, so an output that
    did either cannot claim to have preserved what it just moved. It is then a
    decode stage, and carries a decode stage's obligations —

    * every emitted record cites the input range it came from in ``spans``;
    * every removed range is marked :class:`~zpf.Undecoded` with
      ``reason="dropped"`` — content of the stream that was taken out, which
      since `0.17` is a different statement from ``skipped`` and is what makes
      the seam duty checkable — so the coverage guarantee holds over the whole
      input;
    * ``decoder_id`` names a **layer**, not a stage, so the input's are
      inherited and their Decoder Descriptors re-declared. This stage
      declares no decoder of its own — a filtered HTTP message is still an
      HTTP message — and identifies itself through ``produced_by``.

    A reordering stage's spans will *not* ascend with stored order. That is
    expected: coverage depends on which ranges are covered, not on the order
    they appear in.

    **Which kind of stage this is, is decided per run, and `0.19` is what made
    that sayable.** This docstring used to open by declaring the output "not a
    pass-through, however byte-preserving it looks", which was true while the
    discriminator was which *option* was present — a pass-through carried
    ``origin`` and this function never wrote one. Since `0.19` the
    discriminator is whether a record's spans are **identity**, the same range
    in as out, and with ``keep=None, reorder=None`` every span this writes is
    exactly that. So an unfiltered, unreordered run *is* a pass-through by the
    specification's own test, and a filtering or reordering one is not. Nothing
    about the output changed; what changed is that the file now says which,
    and says it per record rather than per file.

    **The gap this used to fill is now the standard's.** Through ``0.14``
    nothing required a block at a drop point: the MUST NOT was written about
    spans crossing an *input's* break, and a filter's spans cross nothing.
    This library emitted one anyway, behind a ``mark_gaps`` switch, and
    reported the hole as
    `zipline#78 <https://github.com/adamkjonsson/zipline/issues/78>`_.
    ``0.15`` closed it: a stage **MUST** emit a Discontinuity between two
    adjacent units of its own output wherever those two do not join, and a
    filter whose dropped records break its survivors apart is one of the
    three shapes named. So the switch is gone — an always-conformant writer
    has no business offering output that is not.

    The two shapes are distinguished by **width**, and the distinction is
    the specification's. A drop withheld content of known extent, so the
    block declares it (``reason="records-dropped"``); a reorder withheld
    nothing, and what lies between two units that were never adjacent is not
    a hole to be counted, so it declares none (``reason="reordered"``).

    Through ``0.12`` a transform producing records
    without decoding them had no ``params_digest`` to record its own
    configuration in, so the output stated *what* it came from but not
    *how*; ``0.14`` adds ``transform_params_digest`` to the File Header for
    precisely this case, and :func:`merge_files` takes it too.

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
        transform_params_digest: Hash of this stage's configuration — the
            filter predicate, the ordering, whatever decides the output.
            Recording it is what makes the rewrite reproducible.
        input_ref: How to describe the input in the output's Source — see
            :class:`~zpf.InputRef`. Both halves default.
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
            transform_params_digest=transform_params_digest,
            comment=comment,
        )
        with writer:
            ref = input_ref or InputRef()
            derived = writer.derive_from(reader, uri=ref.uri, digest=ref.digest)
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
    blocks = list(session.stream_blocks(pid))
    records = [block for block in blocks if isinstance(block, Record)]
    ranges = session.ranges(pid)
    cited = {id(record): span for record, span in zip(records, ranges, strict=True)}
    # The input's breaks, as ranges of its offset space that hold no bytes.
    # They are holes in the space, not regions this stage declined: marking
    # them Undecoded would claim bytes exist upstream to go and fetch.
    holes: list[tuple[int, int]] = []
    cursor = 0
    for block in blocks:
        if isinstance(block, Discontinuity):
            holes.append((cursor, cursor + (block.width or 0)))
            cursor += block.width or 0
        else:
            cursor += len(block.payload)
    survivors = [record for record in records if keep is None or keep(record)]
    if reorder is not None:
        survivors = list(reorder(survivors))
    kept_set = {id(record) for record in survivors}
    # Duty 1 of the specification's MUST NOT: this stage reads an input that
    # may carry breaks, so it may not emit a unit spanning one without
    # emitting its own. It re-emits records rather than merging them, so it
    # satisfies that by carrying every input break forward.
    inherited = _inherited_breaks(blocks, kept_set)
    previous: Record | None = None
    for record in survivors:
        carried = 0
        for break_block in inherited.get(id(record), ()):
            out.discontinuity(
                handle, width=break_block.width, reason=break_block.reason,
                comment=break_block.comment,
            )
            carried += break_block.width or 0
        if previous is not None:
            _declare_seam(out, handle, cited[id(previous)][1], cited[id(record)][0], carried)
        previous = record
        off_start, off_end = cited[id(record)]
        out.record(
            handle,
            ts=record.timestamp,
            payload=record.payload,
            source=derived.source,
            flags=record.flags,
            decoded=Decoded(
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
            ),
        )
    # A declared width is a range of the input's offset space that holds no
    # bytes at all. Coverage still has to account for it — the guarantee is
    # about the whole stream — but not as `skipped`, whose class is `bytes`
    # and would send a consumer upstream after bytes that were never there.
    # `gap` is the "hole" class: nothing exists here to go and fetch.
    for start, end in holes:
        if start < end:
            writer.undecoded(
                derived.source, session.session_id, pid, start, end, reason="gap"
            )
    kept = sorted([cited[id(record)] for record in survivors] + holes)
    extent = ranges[-1][1] if ranges else 0
    # `dropped`, not `skipped`, and the distinction is normative since 0.17.
    # Both are bytes-class; what separates them is what this stage did. A
    # filter *removed content of the stream*, so the survivors either side may
    # not join — which is what the Discontinuity `_declare_seam` emits says.
    # `skipped` is for withholding something that carried no content, and
    # writing it here would produce a conformant-looking file whose seam duty
    # no checker could test. 0.18 made the spelling a MUST for exactly that
    # reason; any further specificity belongs in `comment`.
    for start, end in complement(kept, extent):
        writer.undecoded(
            derived.source, session.session_id, pid, start, end, reason="dropped"
        )


def _declare_seam(
    out: SessionWriter, handle: ParticipantHandle, ends: int, begins: int, carried: int
) -> None:
    """Declare this stage's own break between two survivors, if it owes one.

    The duty is *do these two join?*, and it is about content **this stage**
    withheld. The input's own breaks are already back between the same two
    units, so their declared widths are subtracted before asking: a seam
    fully explained by a carried-forward block owes nothing more, and
    declaring it again would count the same missing bytes twice.

    Args:
        out: The output session writer.
        handle: The output participant.
        ends: Where the previous survivor's input range ended.
        begins: Where this survivor's input range begins.
        carried: Total declared width of the input breaks just re-emitted
            at this seam.

    """
    if begins < ends:
        # Reordered: these two were never adjacent, and what lies between
        # them is not a hole to be counted. No width, per the specification.
        out.discontinuity(handle, reason="reordered")
        return
    withheld = begins - ends - carried
    if withheld > 0:
        # Dropped: content of known extent did not reach the output, and the
        # extent is exactly what the survivors' input ranges leave between
        # them once the input's own declared breaks are accounted for.
        out.discontinuity(handle, width=withheld, reason="records-dropped")


def _inherited_breaks(
    blocks: Sequence[Record | Discontinuity], kept: set[int]
) -> dict[int, list[Discontinuity]]:
    """Attach each input break to the surviving record that follows it.

    Carrying a break forward means putting it back between the same two
    units. When the record that followed it was dropped, the break travels
    to the next survivor: the join it denies is still a join being made.

    Args:
        blocks: One participant's records and breaks, in stored order.
        kept: ``id()`` of every record that survived.

    Returns:
        Per surviving record's ``id()``, the breaks to emit before it.

    """
    attached: dict[int, list[Discontinuity]] = {}
    pending: list[Discontinuity] = []
    for block in blocks:
        if isinstance(block, Discontinuity):
            pending.append(block)
        elif id(block) in kept and pending:
            attached[id(block)] = pending
            pending = []
    return attached

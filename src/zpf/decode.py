"""The decode-stage orchestrator — open an input, wire an output, decode.

Writing a decode stage by hand means repeating the same scaffolding every
time: hash the input, declare it as a ``zpf-input`` Source, copy the input's
time base onto the output header, re-declare every participant under the
same id so offsets line up, and thread the source/session/participant ids
through every span. :func:`decode_stage` does all of that once::

    with zpf.decode_stage(
        "raw.zpf", "decoded.zpf",
        decoder=("http/1.1", "1.0"),
        produced_by="http-decode 1.0",
        produced_at=1_700_000_000,
    ) as dec:
        for stream in dec.streams():
            for seg in stream.segments():
                for start, end, kind in split_messages(seg.data):
                    dec.record(
                        stream,
                        seg.data[start:end],
                        ts=seg.ts,
                        content_type=f"dec:http-{kind}",
                        cites=(seg.off_start + start, seg.off_start + end),
                    )

Each :class:`DecodeStream` pairs one input stream with the output
participant that stands for it, so a citation can only ever name the stream
it came from. To keep your own :func:`zpf.create` call instead, use the
lower-level :meth:`zpf.FileWriter.derive_from`, which builds the same
scaffolding without owning the control flow.
"""

from __future__ import annotations

from typing import IO, TYPE_CHECKING

from zpf._intervals import complement, intersections
from zpf.blocks import InputExtent, Span
from zpf.errors import ZpfError
from zpf.reader import FileReader
from zpf.reassembly import Gap
from zpf.writer import create

if TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Iterator, Sequence
    from datetime import datetime
    from types import TracebackType
    from typing import Self

    from zpf.blocks import Participant, RecordFlags
    from zpf.reassembly import Datagram, Segment, StreamView
    from zpf.writer import (
        DecoderHandle,
        DerivedInput,
        FileWriter,
        ParticipantHandle,
        SessionWriter,
        SourceHandle,
    )

    _Cites = Span | tuple[int, int] | Iterable[Span | tuple[int, int]] | None

_PAIR = 2


class DecodeStream:
    """One input stream, paired with the output participant standing for it.

    Handed out by :meth:`DecodeStage.streams`. Delegates the reading side to
    the underlying :class:`~zpf.reassembly.StreamView` — :meth:`segments`,
    :meth:`chunks`, :meth:`datagrams` — so a decoder iterates it directly.

    Attributes:
        view: The input stream's reassembly view, already bound to cite the
            ``zpf-input`` source.
        handle: The output participant re-declared for this stream.
        session: The output session writer this stream's records go to.

    """

    def __init__(
        self, view: StreamView, handle: ParticipantHandle, session: SessionWriter
    ) -> None:
        self.view = view
        self.handle = handle
        self.session = session

    @property
    def participant(self) -> Participant:
        """The input participant descriptor."""
        return self.view.participant

    @property
    def session_id(self) -> int:
        """The stream's session id, shared by the input and the output."""
        return self.view.participant.session_id

    @property
    def pid(self) -> int:
        """The stream's participant id, shared by the input and the output."""
        return self.view.participant.participant_id

    @property
    def is_stream_oriented(self) -> bool:
        """Whether the input is a byte stream (vs. a sequence of datagrams)."""
        return self.view.is_stream_oriented

    @property
    def off_start(self) -> int:
        """The input's first available logical offset."""
        return self.view.off_start

    def segments(self) -> Iterator[Segment]:
        """Iterate the input's contiguous runs. See :meth:`zpf.StreamView.segments`."""
        return self.view.segments()

    def chunks(self) -> Iterator[Segment | Gap]:
        """Iterate the input's runs and holes. See :meth:`zpf.StreamView.chunks`."""
        return self.view.chunks()

    def datagrams(self) -> Iterator[Datagram]:
        """Iterate the input's datagrams. See :meth:`zpf.StreamView.datagrams`."""
        return self.view.datagrams()

    def reassembled(self) -> bytes:
        """Return the whole input stream. See :meth:`zpf.StreamView.reassembled`."""
        return self.view.reassembled()

    def cite(self, off_start: int, off_end: int) -> Span:
        """Cite a range of this input stream, with every id filled in.

        Args:
            off_start: First logical offset to cite.
            off_end: One past the last offset to cite.

        Returns:
            A :class:`~zpf.blocks.Span` naming this stream's range.

        """
        return self.view.cite(off_start, off_end)


class DecodeStage:
    """An open decode stage: an input being read, an output being written.

    Created by :func:`decode_stage`, normally as a context manager. A clean
    exit closes the output (writing its End block) and then the input, if
    this stage opened it.

    Attributes:
        reader: The input file.
        writer: The output file.
        derived: The scaffolding built from the input — source handle,
            session writers, and the participant map.

    """

    def __init__(
        self,
        reader: FileReader,
        writer: FileWriter,
        derived: DerivedInput,
        decoder: DecoderHandle | None,
        *,
        owns_reader: bool,
        fill_undecoded: bool = True,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.derived = derived
        self._decoder = decoder
        self._owns_reader = owns_reader
        self._fill_undecoded = fill_undecoded
        self._streams: tuple[DecodeStream, ...] | None = None
        self._cited: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self._marked: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close(end=exc_type is None)

    @property
    def source(self) -> SourceHandle:
        """The ``zpf-input`` Source handle the output cites."""
        return self.derived.source

    @property
    def decoder(self) -> DecoderHandle | None:
        """The declared Decoder applied to this stage's records."""
        return self._decoder

    def streams(self) -> tuple[DecodeStream, ...]:
        """Return every input stream, paired with its output participant.

        One :class:`DecodeStream` per participant of every input session,
        in declaration order.

        Returns:
            The stage's streams.

        """
        if self._streams is None:
            paired: list[DecodeStream] = []
            for session in self.reader.sessions():
                out = self.derived.sessions[session.session_id]
                for view in session.reassemble():
                    handle = self.derived.handle(view.participant)
                    paired.append(DecodeStream(view.cited_as(self.derived.source), handle, out))
            self._streams = tuple(paired)
        return self._streams

    def record(
        self,
        stream: DecodeStream,
        payload: bytes = b"",
        *,
        ts: int,
        content_type: str | None = None,
        cites: _Cites = None,
        spans: Sequence[Span] = (),
        decoder: DecoderHandle | None = None,
        flags: RecordFlags | int = 0,
    ) -> None:
        """Write one decoded record for ``stream``.

        The output participant, the stage's decoder, and the ids inside
        every cited span are filled in from ``stream``.

        Args:
            stream: The input stream this record was decoded from.
            payload: The decoded bytes.
            ts: Record time in the file's ticks — per the specification's
                timestamp rule, the completion time of the last input
                record the payload came from (a run's
                :attr:`Segment.ts <zpf.reassembly.Segment.ts>`).
            content_type: ``dec:``/``mime:``/``prim:`` payload label.
            cites: The input range this record was built from: an
                ``(off_start, off_end)`` pair, a ready
                :class:`~zpf.blocks.Span`, or a sequence of either.
            spans: Extra spans to append, already built.
            decoder: Override the stage's decoder for this record.
            flags: Record flags.

        """
        all_spans = tuple(spans) + _as_spans(stream, cites)
        self._track(self._cited, all_spans)
        stream.session.record(
            stream.handle,
            ts=ts,
            payload=payload,
            decoder=decoder if decoder is not None else self._decoder,
            content_type=content_type,
            spans=all_spans,
            flags=flags,
        )

    def undecoded(
        self,
        stream: DecodeStream,
        off_start: int,
        off_end: int,
        *,
        reason: str | None = None,
        decoder: DecoderHandle | None = None,
        comment: str | None = None,
    ) -> None:
        """Mark a range of ``stream`` this stage did not decode.

        Args:
            stream: The input stream the range belongs to.
            off_start: First logical offset left undecoded.
            off_end: One past the last offset left undecoded.
            reason: Why, e.g. ``"undecodable"``, ``"gap"``,
                ``"truncated"``.
            decoder: The decoder that declined the region — for a session
                with more than one decoder. Defaults to the stage's decoder,
                like :meth:`record`. (The ``fill_undecoded`` auto-fill has no
                basis to pick, so it always uses the stage's decoder.)
            comment: Free-text note.

        """
        if off_start < off_end:
            self._marked.setdefault((stream.session_id, stream.pid), []).append(
                (off_start, off_end)
            )
        self.writer.undecoded(
            self.derived.source,
            stream.session_id,
            stream.pid,
            off_start,
            off_end,
            reason=reason,
            decoder=decoder if decoder is not None else self._decoder,
            comment=comment,
        )

    def discontinuity(
        self,
        stream: DecodeStream,
        *,
        width: int | None = None,
        reason: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Mark a break in **this stage's own output** for ``stream``.

        The mirror image of :meth:`undecoded`, and the pair are easy to
        confuse. An Undecoded block is about the *input*: bytes over there
        this stage did not decode. This is about the output: the units
        either side of it do not join, whatever their offsets say.

        Emit it at the point in the output where the break belongs — stored
        order is what places it — and note that it discharges **no** coverage
        obligation. A stage that both fails to decode an input region and
        breaks its output owes an Undecoded block *and* this.

        Args:
            stream: The input stream whose output participant breaks. The
                block is written against this stage's own ids.
            width: The break's extent in the output's offset space, when it
                can be counted; leave it out when it cannot. Absent means
                unknowable, which is not a declared ``0``.
            reason: ``"tls-record-lost"``, ``"decrypt-failed"``,
                ``"stream-gap"``, … (open vocabulary).
            comment: Free-text note.

        """
        self.derived.sessions[stream.session_id].discontinuity(
            self.derived.participants[(stream.session_id, stream.pid)],
            width=width,
            reason=reason,
            comment=comment,
        )

    def close(self, *, end: bool = True) -> None:
        """Close the output, then the input if this stage opened it.

        On a clean close with auto-fill on, every input offset the decoder
        left neither cited nor marked is written as an ``Undecoded`` block
        first — ``gap`` for a reassembly hole, ``skipped`` for data the
        decoder passed over — so the output satisfies the coverage guarantee
        by construction.

        Args:
            end: Write the output's End block (marks it complete). Auto-fill
                runs only when ``end`` is true; a failed stage stays honestly
                incomplete and unpadded.

        Raises:
            ZpfError: If the decoder both cited and explicitly marked the
                same input offset (auto-fill will not silently override it).

        """
        if self._closed:
            return
        self._closed = True
        try:
            if end and self._fill_undecoded:
                try:
                    self._autofill()
                except BaseException:
                    self.writer.close(end=False)  # failed stage: no End block
                    raise
            if end:
                self._declare_extents()
            self.writer.close(end=end)
        finally:
            if self._owns_reader:
                self.reader.close()

    def _track(
        self,
        into: dict[tuple[int, int], list[tuple[int, int]]],
        spans: tuple[Span, ...],
    ) -> None:
        """Record the intervals of ``spans`` that cite this stage's input source."""
        source_id = self.derived.source.source_id
        for span in spans:
            if span.source_id == source_id and span.off_start < span.off_end:
                into.setdefault((span.session_id, span.participant_id), []).append(
                    (span.off_start, span.off_end)
                )

    def _declare_extents(self) -> None:
        """End each output session, declaring how long its inputs were.

        The stage knows every input stream's extent by the time it closes,
        and the specification says a derived file SHOULD declare them: they
        are what lets a consumer check the coverage guarantee from the output
        alone, without opening the input to measure it. A trailing gap is
        invisible without them — coverage that stops early looks exactly like
        a stream that was that short.

        Measured with the same rule the coverage check uses, so the two
        cannot disagree.
        """
        by_session: dict[int, list[InputExtent]] = {}
        for stream in self.streams():
            extent, _gaps = _extent_and_gaps(stream.view)
            by_session.setdefault(stream.session_id, []).append(
                InputExtent(
                    source_id=self.derived.source.source_id,
                    session_id=stream.session_id,
                    participant_id=stream.pid,
                    extent=extent,
                )
            )
        for session_id, out in self.derived.sessions.items():
            out.end(input_extents=by_session.get(session_id, []))

    def _autofill(self) -> None:
        """Mark every uncovered input offset as Undecoded, gaps included."""
        for stream in self.streams():
            key = (stream.session_id, stream.pid)
            cited = sorted(self._cited.get(key, []))
            marked = sorted(self._marked.get(key, []))
            clash = intersections(cited, marked)
            if clash:
                start, end = clash[0]
                msg = (
                    f"input stream (session {key[0]}, pid {key[1]}): [{start}, {end}) "
                    "is both decoded and explicitly marked Undecoded; auto-fill will "
                    "not override an explicit marker"
                )
                raise ZpfError(msg)
            extent, gaps = _extent_and_gaps(stream.view)
            uncovered = complement(sorted(cited + marked), extent)
            for start, end, reason in _label_fills(uncovered, gaps):
                self.writer.undecoded(
                    self.derived.source,
                    stream.session_id,
                    stream.pid,
                    start,
                    end,
                    reason=reason,
                    decoder=self._decoder,
                )


def decode_stage(
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | FileReader,
    sink: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    decoder: str | tuple[str, str | None] | DecoderHandle,
    produced_by: str,
    produced_at: int | datetime,
    proto: str | None = None,
    uri: str | None = None,
    digest: str | None = None,
    comment: str | None = None,
    fill_undecoded: bool = True,
) -> DecodeStage:
    """Open an input and scaffold a decode-stage file derived from it.

    Copies the input's ``tick_hz`` and ``time_epoch`` onto the output
    header, declares the input as a ``zpf-input`` Source (hashing it when no
    ``digest`` is given), declares the decoder, and mirrors the input's
    sessions and participants under the same ids.

    Args:
        source: The input ``.zpf`` — a path, a seekable stream, or an
            already-open :class:`~zpf.FileReader` (which stays open and is
            not closed by this stage). Open it yourself when you want
            non-default read options, such as ``strict=True``.
        sink: Where to write the decode-stage file.
        decoder: The decoder to declare: a name, a ``(name, version)``
            pair, or a handle already declared on another writer's terms.
        produced_by: Tool + version doing the decoding (required of a
            derived file).
        produced_at: Build time (required of a derived file); Unix seconds,
            or a timezone-aware :class:`~datetime.datetime`.
        proto: Protocol for the output sessions, e.g. ``"http"``; defaults
            to the input's.
        uri: Where the input lives; defaults to the path it was opened
            from.
        digest: Content hash of the input; SHA-256 of it when omitted.
        comment: Free-text note for the File Header.
        fill_undecoded: On a clean close, mark every input offset the
            decoder left uncovered as ``Undecoded`` (``gap`` for
            reassembly holes, ``skipped`` for data the decoder passed over),
            so the output satisfies the coverage guarantee by construction.
            On by default; set false to emit only the ``Undecoded`` blocks
            you write yourself. Auto-fill never uses ``undecodable``, which
            is reserved for the decoder's explicit "tried and failed".

    Returns:
        An open :class:`DecodeStage`, also a context manager.

    Raises:
        ZpfError: If the input has no File Header.

    """
    reader, owns_reader = _open_input(source)
    try:
        header = reader.header
        if header is None:
            msg = "input file has no File Header to derive from"
            raise ZpfError(msg)
        writer = create(
            sink,
            tick_hz=header.tick_hz,
            time_epoch=header.time_epoch,
            produced_by=produced_by,
            produced_at=produced_at,
            comment=comment,
        )
    except BaseException:
        if owns_reader:
            reader.close()
        raise
    try:
        derived = writer.derive_from(reader, uri=uri, digest=digest, proto=proto)
        handle = _declare_decoder(writer, decoder)
    except BaseException:
        writer.close(end=False)
        if owns_reader:
            reader.close()
        raise
    return DecodeStage(
        reader, writer, derived, handle, owns_reader=owns_reader, fill_undecoded=fill_undecoded
    )


def _open_input(
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | FileReader,
) -> tuple[FileReader, bool]:
    """Return the input reader, and whether this stage owns (must close) it."""
    if isinstance(source, FileReader):
        return source, False
    return FileReader(source), True


def _declare_decoder(
    writer: FileWriter, decoder: str | tuple[str, str | None] | DecoderHandle
) -> DecoderHandle:
    """Declare the stage's decoder from a name, a (name, version) pair, or a handle."""
    if isinstance(decoder, str):
        return writer.add_decoder(decoder)
    if isinstance(decoder, tuple):
        name, version = decoder
        return writer.add_decoder(name, version=version)
    return decoder


def _as_spans(stream: DecodeStream, cites: _Cites) -> tuple[Span, ...]:
    """Normalise the ``cites`` shorthand into spans over ``stream``."""
    if cites is None:
        return ()
    if isinstance(cites, Span):
        return (cites,)
    if _is_offset_pair(cites):
        start, end = cites
        return (stream.cite(start, end),)
    spans: list[Span] = []
    for cite in cites:
        if isinstance(cite, Span):
            spans.append(cite)
        elif _is_offset_pair(cite):
            start, end = cite
            spans.append(stream.cite(start, end))
        else:
            msg = (
                "cites entries must be (off_start, off_end) pairs or zpf.Span "
                f"objects, got {cite!r}"
            )
            raise ZpfError(msg)
    return tuple(spans)


def _is_offset_pair(value: object) -> bool:
    """Whether ``value`` is a plain ``(off_start, off_end)`` pair."""
    return (
        isinstance(value, (tuple, list))
        and len(value) == _PAIR
        and all(isinstance(item, int) for item in value)
    )


def _extent_and_gaps(view: StreamView) -> tuple[int, list[tuple[int, int]]]:
    """Return an input stream's hole-inclusive extent and its gap ranges.

    Matches the extent model :func:`zpf.check_coverage` uses: the last
    offset of the reassembled space for a stream-oriented participant, the
    cumulative payload length for a packet-oriented one (which has no gaps).
    """
    if not view.is_stream_oriented:
        extent = 0
        for datagram in view.datagrams():
            extent = datagram.off_end
        return extent, []
    extent = 0
    gaps: list[tuple[int, int]] = []
    for chunk in view.chunks():
        extent = chunk.off_end
        if isinstance(chunk, Gap):
            gaps.append((chunk.off_start, chunk.off_end))
    return extent, gaps


def _label_fills(
    uncovered: list[tuple[int, int]], gaps: list[tuple[int, int]]
) -> Iterator[tuple[int, int, str]]:
    """Split uncovered ranges at gap boundaries, labelling each part's reason.

    A part inside a reassembly :class:`~zpf.reassembly.Gap` is a
    ``"gap"``; anything else is data the decoder passed over, marked
    ``"skipped"``. Auto-fill never emits ``"undecodable"`` — that is the
    decoder's own claim that it *tried and failed*, which only an explicit
    :meth:`DecodeStage.undecoded` call can make.
    """
    ordered = sorted(gaps)
    for start, end in uncovered:
        position = start
        for gap_start, gap_end in ordered:
            lo, hi = max(position, gap_start), min(end, gap_end)
            if lo >= hi:
                continue
            if position < lo:
                yield position, lo, "skipped"
            yield lo, hi, "gap"
            position = hi
        if position < end:
            yield position, end, "skipped"

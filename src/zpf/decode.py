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

from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from zpf._intervals import complement, intersections
from zpf.blocks import InputExtent, OutputLayer, Span
from zpf.errors import SemanticError, ZpfError
from zpf.reader import FileReader
from zpf.reassembly import Gap
from zpf.writer import InputRef, create

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


@dataclass(frozen=True)
class Hints:
    """TCP ordering hints for a record at the **transport** layer.

    Passed to :meth:`DecodeStage.record` as ``hints=``. A transport stream's
    requirements bind on the layer, not on where its bytes came from, so a
    sessionization stage carries these exactly as a capture's reassembled
    stream does — which is the point of its output being a transport layer
    at all.

    Attributes:
        seq_start: Absolute sequence number of the run's first byte.
        ack: Cumulative acknowledgement in force, where known.

    """

    seq_start: int | None = None
    ack: int | None = None


@dataclass(frozen=True)
class Seam:
    """A seam between two of a stage's own records that does **not** join.

    The *writing* face of a :class:`~zpf.blocks.Discontinuity`, and the
    counterpart of :class:`zpf.Break`, which is the reading face: a
    :class:`Seam` says what to declare, a :class:`~zpf.Break` reports what
    was declared, and only the second carries an offset — a seam's position
    is wherever the record it is passed with lands.

    Passed to :meth:`DecodeStage.record` as ``seam=``, which emits the
    :class:`~zpf.blocks.Discontinuity` in the right place for you.

    Attributes:
        width: The break's extent in this stream's offset space, when the
            stage knows it — a filter's dropped payloads, a QUIC stream's
            counted offsets. Absent means *unknowable*, which contributes 0
            to the positional arithmetic and still says the two do not
            join: the honest answer where a lost TLS record's plaintext
            length cannot be recovered. A **reordering** stage always
            leaves it absent, because what lies between two units that were
            never adjacent is not a hole to be counted.
        reason: Why they do not join, e.g. ``"records-dropped"``,
            ``"reordered"``, ``"tls-record-lost"``. Open vocabulary.

    """

    width: int | None = None
    reason: str | None = None


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
        # Streams that have emitted a record, so a seam has two sides.
        self._emitted: set[int] = set()
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

        **Check** :attr:`DecodeStream.is_stream_oriented` **rather than
        assuming** :meth:`~zpf.StreamView.segments`. A stage's input shape
        follows its input's *records*, not the transport underneath them: a
        **decoded** file is always packet-oriented, because decoding
        replaces ``seq``/``ack`` with positional offsets and
        :meth:`~zpf.FileWriter.derive_from` deliberately does not copy
        ``isn``. So a decoder that works on a transport input raises on the
        output of the stage before it, which is where chained stages bite.
        A stage emitting a **transport** layer is the exception — its
        records carry :class:`Hints`, so its output is stream-oriented like
        any capture.

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

    def record(  # noqa: PLR0913
        # Eleven parameters, one over the limit. Two of them (seam, hints)
        # are already bundles, so the count is the stage's genuine surface
        # rather than a flat spill. Restructuring both record() signatures is
        # tracked for v0.3.0, where a break is allowed; until then this is a
        # suppression rather than a design.
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
        seam: Seam | None = None,
        hints: Hints | None = None,
        comment: str | None = None,
    ) -> None:
        """Write one decoded record for ``stream``.

        The output participant, the stage's decoder, and the ids inside
        every cited span are filled in from ``stream``.

        **The seam.** A stage MUST declare a break between two adjacent
        units of its own output wherever those two do not join, and the
        question is one only the producer can answer — it rests on what the
        stage did with its input, and most of it is not mechanically
        decidable. So this asks, once per record, rather than leaving a
        block to be remembered: pass ``joins_previous=False`` where the
        content this record represents did not run continuously on from the
        previous one, and the Discontinuity is emitted for you.

        The default is ``True`` because the common seam is a join: framing
        bytes, a nonce and a tag left undecoded between two units withhold
        nothing, and the content either side runs straight on. What is *not*
        a join: a ``hole``-class region between them, content the stage
        declined or dropped, and units it reordered so that these two were
        never neighbours.

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
            seam: The break between this record and the previous one of the
                same stream, when they do not join. Omit where they do.
                Ignored for a stream's first record, which has no seam.
            hints: TCP ordering hints, for a stage emitting a **transport**
                layer. They bind on the layer rather than on provenance, so
                a sessionization stage's ``zpf``-sourced output carries them
                exactly as a capture's reassembled stream does. A decoded
                record has no use for them — its offsets are positional.
            comment: Free-text note on the record. **Free text**: nothing
                parses it and no consumer may depend on its shape. A stage
                emitting one record per protocol field may use it to say
                which field a record is, but that is a stopgap — the name is
                load-bearing semantics carried in a field that promises
                none.

        Raises:
            SemanticError: If the record cites no input range.

        """
        all_spans = tuple(spans) + _as_spans(stream, cites)
        if not all_spans:
            # A decode stage's records are *created*, and spans are what say
            # which input range each one corresponds to. Emitting one
            # without them claims it was re-emitted unchanged, which this
            # stage cannot mean, and leaves the region it came from outside
            # the coverage guarantee. The checker cannot catch it: since
            # 0.16 the created-versus-preserved discriminator binds per
            # participant, and this participant's other records carry spans,
            # so the stream is well formed and only the API knows better.
            msg = (
                "a decode stage's record must cite the input range it was built "
                "from; pass cites= or spans=, or emit an Undecoded marker instead"
            )
            raise SemanticError(msg)
        self._track(self._cited, all_spans)
        if seam is not None and stream.pid in self._emitted:
            stream.session.discontinuity(
                stream.handle, width=seam.width, reason=seam.reason
            )
        self._emitted.add(stream.pid)
        stream.session.record(
            stream.handle,
            ts=ts,
            payload=payload,
            decoder=decoder if decoder is not None else self._decoder,
            content_type=content_type,
            spans=all_spans,
            flags=flags,
            seq_start=None if hints is None else hints.seq_start,
            ack=None if hints is None else hints.ack,
            comment=comment,
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


def decode_stage(  # noqa: PLR0913
    # The stage's own knobs, one over the limit. Same reasoning as
    # SessionWriter.record(): bundling some into a struct to satisfy a count
    # would obscure what a stage is configured by, not clarify it.
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | FileReader,
    sink: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    decoder: str | tuple[str, str | None] | DecoderHandle,
    produced_by: str,
    produced_at: int | datetime,
    output_layer: OutputLayer | int = OutputLayer.DECODED,
    proto: str | None = None,
    sequenced: bool = False,
    input_ref: InputRef | None = None,
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
        output_layer: The layer this stage's decoder emits. Leave it at
            :attr:`~zpf.OutputLayer.DECODED` for an app decoder. Pass
            :attr:`~zpf.OutputLayer.TRANSPORT` to build a **sessionization
            stage** — a reassembler run over a ``.zpf`` rather than over a
            capture, whose output is ``zpf``-sourced and transport-shaped.
            0.14 could not express that at all: a reassembler had to be
            characterised by the *absence* of a ``decoder_id``, so its
            overlap policy and timeout had nowhere to be recorded. Ignored
            when ``decoder`` is a handle, which was declared already.
        produced_by: Tool + version doing the decoding (required of a
            derived file).
        produced_at: Build time (required of a derived file); Unix seconds,
            or a timezone-aware :class:`~datetime.datetime`.
        proto: Protocol for the output sessions, e.g. ``"http"``; defaults
            to the input's.
        sequenced: Emit each session's records in causal order and mark it
            SEQUENCED, instead of writing them stream by stream. A stage
            decodes one stream at a time, so its natural output order is
            two monologues rather than the conversation the input records;
            with this, records are buffered per session and interleaved by
            their timestamps — which for a decoded record is the completion
            time of the last input record it came from, so the result is
            the input's own timeline.

            Costs: the session is held in memory until it ends, and a
            :class:`~zpf.blocks.Discontinuity` cannot be emitted while it
            is on (its meaning is positional, and reordering is what moves
            it). Each session's ``sequenced_basis`` is derived from the
            input — see :meth:`zpf.FileWriter.derive_from` — and a session
            whose input supports no causal order raises rather than
            claiming one.
        input_ref: How to describe the input in the output's Source — see
            :class:`~zpf.InputRef`. Both halves default: the URI to the path
            the input was opened from, the digest to SHA-256 of its bytes.
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
        ref = input_ref or InputRef()
        derived = writer.derive_from(
            reader, uri=ref.uri, digest=ref.digest, proto=proto, sequenced=sequenced
        )
        handle = _declare_decoder(writer, decoder, output_layer)
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
    writer: FileWriter,
    decoder: str | tuple[str, str | None] | DecoderHandle,
    output_layer: OutputLayer | int,
) -> DecoderHandle:
    """Declare the stage's decoder from a name, a (name, version) pair, or a handle."""
    if isinstance(decoder, str):
        return writer.add_decoder(decoder, output_layer=output_layer)
    if isinstance(decoder, tuple):
        name, version = decoder
        return writer.add_decoder(name, version=version, output_layer=output_layer)
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

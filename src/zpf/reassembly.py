"""Reassembly views over a participant's stream — the ergonomic decoder input.

A decoder consumes one participant stream at a time. Two shapes exist, and
they want different iteration idioms:

* A **stream-oriented** participant (TCP: the handshake was seen, or its
  records carry ``seq_start``) is a byte stream that may have holes where a
  segment was lost. :meth:`StreamView.chunks` walks it as contiguous
  :class:`Segment` runs with the :class:`Gap` between them made explicit, so
  a decoder never silently welds bytes across a hole. :class:`Segment` and
  :class:`Gap` carry **logical stream offsets** — 0-based positions where
  byte 0 is ``isn + 1`` (or the first captured byte when the handshake was
  missed) — the same coordinate system the coverage guarantee is written in.
* A **packet-oriented** participant (UDP, or any hint-less stream) is a
  sequence of whole datagrams; :meth:`StreamView.datagrams` yields them one
  record at a time, with cumulative byte offsets.

The view does not re-run reassembly: a conformant ``.zpf`` already holds
in-order, non-overlapping bytes (the producer resolved retransmits and
overlap under a favor-old policy before writing). The view coalesces those
records and surfaces the holes the producer could not fill.

Obtain views from :meth:`zpf.SessionReader.reassemble`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from zpf.blocks import Discontinuity, OutputLayer, Record, Span
from zpf.errors import SemanticError, ZpfError
from zpf.order import SEQ_SPACE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from zpf.blocks import Decoder, Participant
    from zpf.writer import SourceHandle


def stream_layer(
    records: Sequence[Record], decoders: Mapping[int, Decoder]
) -> OutputLayer | int:
    """Resolve the layer of one participant's stream.

    The rule is *layer = decoder present ? that decoder's* ``output_layer``
    *: transport*. Both halves matter: reassembly is a decoder too, so
    carrying a ``decoder_id`` no longer means a record is decoded, and the
    answer needs the Decoder table rather than the records alone. That is
    why this takes ``decoders`` and why it replaced the old
    ``is_decoded_stream``, whose signature could not be repaired.

    The layer is a property of the **stream**, so it is resolved once per
    participant rather than per record — which the specification licenses
    only because every record of a participant must resolve to the same
    layer. Where they do not, there is no answer to give and this says so
    rather than picking one: silently choosing would put every offset in
    the participant in the wrong space, which is the exact failure the rule
    exists to prevent.

    Args:
        records: The participant's records, in stored order.
        decoders: The file's declared Decoders, by ``decoder_id``.
            Declare-on-first-use guarantees every id a record references is
            already in here.

    Returns:
        The stream's layer. An :class:`~zpf.blocks.OutputLayer` where the
        declared value is one this version defines, and the raw ``int``
        where it is not — a caller cannot compute an offset space from that
        and must not guess, but the value is reported rather than hidden. A
        participant with no records is ``TRANSPORT``, the no-decoder answer.

    Raises:
        SemanticError: If the records resolve to two different layers, or
            reference a ``decoder_id`` the file never declared.

    """
    layers: set[OutputLayer | int] = set()
    for record in records:
        if record.decoder_id is None:
            layers.add(OutputLayer.TRANSPORT)
            continue
        decoder = decoders.get(record.decoder_id)
        if decoder is None:
            msg = (
                f"record references undeclared decoder_id {record.decoder_id}, "
                f"so its layer cannot be resolved"
            )
            raise SemanticError(msg)
        layers.add(decoder.output_layer)
    if not layers:
        return OutputLayer.TRANSPORT
    if len(layers) > 1:
        named = ", ".join(sorted(layer_name(layer) for layer in layers))
        msg = (
            f"participant mixes layers ({named}), so its offset space has no "
            f"single definition; a reader MUST NOT pick one"
        )
        raise SemanticError(msg)
    return layers.pop()


def layer_name(layer: OutputLayer | int) -> str:
    """Name a layer for a diagnostic, without pretending to know an unknown one."""
    return layer.name.lower() if isinstance(layer, OutputLayer) else f"output_layer {int(layer)}"


def record_ranges(
    participant: Participant,
    blocks: Sequence[Record | Discontinuity],
    layer: OutputLayer | int,
) -> tuple[tuple[int, int], ...]:
    """Compute the offset range each record occupies in its own stream.

    Each layer has its own offset space, and which one applies is decided
    by the layer, not by the presence of hints:

    * A **decoded** stream is the concatenation of that participant's
      decoded record payloads in stored order, byte 0 being the first byte
      of the first such record. Record *k* occupies
      ``[Σ(preceding payload_len + preceding declared widths), + its own
      payload_len)``, counting that participant's records **and its
      Discontinuity blocks** in stored order. So it is hole-inclusive
      exactly where a :class:`~zpf.blocks.Discontinuity` declares a
      ``width``, and positional everywhere else — an absent width means the
      break's extent is unknown and contributes 0. Undecoded regions name
      ranges in the *input's* space and contribute nothing here.
    * A **transport** stream is a true position: holes count as if the
      bytes were present, so offsets come from ``seq_start`` against the
      origin (``isn + 1``, or the first captured byte when the handshake
      was missed). A hint-less transport stream has no way to know about
      holes, so it too is positional.

    A Discontinuity is defined for decoded layers only. One appearing in a
    hinted transport stream is left alone here — those offsets are absolute,
    so a width has nothing to add to them — and is diagnosed by the
    conformance checker rather than silently changing the arithmetic.

    Args:
        participant: The participant whose stream this is.
        blocks: Its records **and** Discontinuity blocks, interleaved in
            stored order. Stored order is what defines the space, so the
            blocks' positions among the records are what make their widths
            terms.
        layer: The stream's layer, from :func:`stream_layer`. Required
            rather than re-derived, because deriving it needs the file's
            Decoder table and this function is handed one participant's
            blocks. Passing the wrong one silently misplaces every record,
            so there is deliberately no default.

    Returns:
        One ``(off_start, off_end)`` per :class:`~zpf.blocks.Record` in
        ``blocks``, in order — Discontinuity blocks occupy no range of their
        own. A record whose position cannot be placed — a hinted stream's
        record carrying no ``seq_start`` — gets a zero-width range at the
        stream's current end, since it contributes no bytes.

    Raises:
        SemanticError: If ``layer`` is a value this version does not define.
            Both offset spaces are wrong for a layer we cannot name, and the
            specification is explicit that a reader MUST NOT guess one.

    """
    if not isinstance(layer, OutputLayer):
        msg = (
            f"cannot compute an offset space for {layer_name(layer)}: the value is "
            f"not one this version defines, and guessing a layer would read the "
            f"stream in the wrong space"
        )
        raise SemanticError(msg)
    records = [block for block in blocks if isinstance(block, Record)]
    decoded = layer is OutputLayer.DECODED
    origin: int | None = None
    if not decoded:
        if participant.isn is not None:
            origin = (participant.isn + 1) % SEQ_SPACE
        else:
            origin = next(
                (r.seq_start for r in records if r.seq_start is not None), None
            )
    if origin is None:
        cursor = 0
        positional: list[tuple[int, int]] = []
        for block in blocks:
            if isinstance(block, Discontinuity):
                cursor += block.width or 0
                continue
            positional.append((cursor, cursor + len(block.payload)))
            cursor += len(block.payload)
        return tuple(positional)
    ranges: list[tuple[int, int]] = []
    end = 0
    for record in records:
        if record.seq_start is None:
            ranges.append((end, end))
            continue
        start = (record.seq_start - origin) % SEQ_SPACE
        stop = start + len(record.payload)
        ranges.append((start, stop))
        end = max(end, stop)
    return tuple(ranges)


def stream_extent(
    participant: Participant,
    blocks: Sequence[Record | Discontinuity],
    layer: OutputLayer | int,
) -> int:
    """Return one past the last offset the participant's stream reaches.

    Args:
        participant: The participant whose stream this is.
        blocks: Its records and Discontinuity blocks, in stored order.
        layer: The stream's layer, from :func:`stream_layer`.

    Returns:
        The stream's extent in its own offset space, 0 when empty.

    Note:
        A Discontinuity **after** the last record does not extend the
        result. A width only displaces the records that follow it, and
        there are none; counting it would claim the stream reaches bytes
        that are by definition missing, which is the opposite of what a
        coverage check wants from this number. The specification defines
        the space through the records' ranges and says nothing about a
        trailing break, so this does not invent an answer for it.

    """
    return max((end for _, end in record_ranges(participant, blocks, layer)), default=0)


@dataclass(frozen=True)
class Segment:
    """A maximal contiguous run of reassembled stream bytes.

    Attributes:
        data: The run's bytes.
        off_start: Logical stream offset of ``data[0]``.
        off_end: One past the run's last offset (``off_start + len(data)``).
        ts: Completion time of the run — the timestamp of the last record
            that contributed to it, in the file's ``tick_hz`` ticks.
        view: The stream this run came from, so the segment can
            :meth:`cite` itself. Excluded from equality and repr.

    """

    data: bytes
    off_start: int
    off_end: int
    ts: int
    view: StreamView | None = field(default=None, compare=False, repr=False)

    def cite(
        self, local_start: int, local_end: int, *, source: SourceHandle | int | None = None
    ) -> Span:
        """Cite a range of this run, in offsets relative to its own start.

        The natural call while parsing: a decoder that found a message at
        ``data[start:end]`` cites ``segment.cite(start, end)`` without
        adding :attr:`off_start` itself.

        Args:
            local_start: First byte to cite, relative to this run's start.
            local_end: One past the last byte, relative to the run's start.
            source: The ``zpf-input`` Source being cited — see
                :meth:`StreamView.cite`.

        Returns:
            A :class:`~zpf.blocks.Span` over the equivalent stream offsets.

        Raises:
            ZpfError: If the range falls outside this run, or the segment
                has no stream view to take ids from.

        """
        if self.view is None:
            msg = (
                "this Segment carries no stream view and cannot cite; take segments "
                "from SessionReader.reassemble() rather than building them by hand"
            )
            raise ZpfError(msg)
        size = len(self.data)
        if not 0 <= local_start <= local_end <= size:
            msg = (
                f"segment-relative range [{local_start}, {local_end}) is outside the "
                f"run's {size} bytes"
            )
            raise ZpfError(msg)
        return self.view.cite(
            self.off_start + local_start, self.off_start + local_end, source=source
        )


@dataclass(frozen=True)
class Gap:
    """A known hole in a stream — bytes a lost segment left missing.

    Attributes:
        off_start: Logical stream offset of the hole's first missing byte.
        off_end: One past the hole's last missing byte.

    """

    off_start: int
    off_end: int


@dataclass(frozen=True)
class Break:
    """A declared discontinuity between two units of a decoded stream.

    The reassembly face of a :class:`~zpf.blocks.Discontinuity`. What it
    asserts is not a length but that the units either side **do not join**:
    a consumer that splices them reads a message that was never sent.

    Distinct from :class:`Gap`, which is a transport hole of known extent —
    bytes that existed and were lost. A break's :attr:`width` may be
    ``None``, meaning the missing extent is not recoverable at all (TLS lost
    a record, and the plaintext length it would have produced cannot be
    known from the ciphertext).

    Attributes:
        off_start: Where the break sits in the stream's offset space.
        width: Its extent, or ``None`` when unknowable. An absent width
            contributes 0 to the arithmetic, so the units either side are
            numerically adjacent — which is exactly why this marker has to
            be surfaced rather than inferred from the offsets.
        reason: Why the stream breaks here, if declared.

    """

    off_start: int
    width: int | None
    reason: str | None


@dataclass(frozen=True)
class Datagram:
    """One record of a packet-oriented stream, consumed as a whole unit.

    Attributes:
        data: The record's payload bytes.
        off_start: The datagram's offset — logical when the stream is
            stream-oriented, cumulative (running payload length) otherwise.
        off_end: One past the datagram's last offset.
        ts: The record's timestamp, in the file's ``tick_hz`` ticks.
        record: The underlying :class:`~zpf.blocks.Record`, for the rare
            decoder that needs ``seq_start``/``ack``/``flags``.

    """

    data: bytes
    off_start: int
    off_end: int
    ts: int
    record: Record


class StreamView:
    """A reassembly view of one participant's stream.

    Handed out by :meth:`zpf.SessionReader.reassemble`. The view is lazy:
    each iteration method re-reads the participant's records, so several
    iterators over the same or different views may be interleaved freely
    (single-threaded use).
    """

    def __init__(
        self,
        participant: Participant,
        blocks: Callable[[], Iterator[Record | Discontinuity]],
        *,
        source: SourceHandle | int | None = None,
    ) -> None:
        self._blocks = blocks
        self._participant = participant
        self._source_id = None if source is None else _source_id_of(source)

    def _records(self) -> Iterator[Record]:
        """Iterate the stream's records, dropping the breaks between them."""
        return (block for block in self._blocks() if isinstance(block, Record))

    @property
    def participant(self) -> Participant:
        """The participant whose stream this view reads."""
        return self._participant

    @property
    def is_stream_oriented(self) -> bool:
        """Whether the stream is a byte stream (vs. a sequence of datagrams).

        True when the handshake fixed an ``isn``, or the stream's records
        carry ``seq_start``; false for a hint-less (packet-oriented) stream.
        """
        if self._participant.isn is not None:
            return True
        first = next(self._records(), None)
        return first is not None and first.seq_start is not None

    @property
    def off_start(self) -> int:
        """The stream's first *available* logical offset.

        0 for a hint-less stream or one whose bytes start at the origin; the
        end of the leading :class:`Gap` when the first captured byte sits
        past ``isn + 1``.
        """
        if not self.is_stream_oriented:
            return 0
        for chunk in self.chunks():
            if isinstance(chunk, Segment):
                return chunk.off_start
        return 0

    def chunks(self) -> Iterator[Segment | Gap]:
        """Iterate the stream as contiguous :class:`Segment` runs with the holes between them.

        Contiguous records are coalesced into one :class:`Segment`; every
        hole — including a leading one before the first captured byte — is
        yielded as a :class:`Gap`, in ascending offset order. Zero-length
        (pure-ACK) records contribute no bytes and are skipped.

        Yields:
            Each :class:`Segment` and :class:`Gap` of the stream, in order.

        Raises:
            ZpfError: If the stream is packet-oriented (iterate
                :meth:`datagrams` instead).

        """
        self._require_stream_oriented("chunks")
        origin: int | None = self._origin()
        cursor = 0
        run = bytearray()
        run_start = 0
        run_ts = 0
        for record in self._records():
            if record.seq_start is None:
                continue  # a stream-oriented stream's records carry seq_start
            if origin is None:
                origin = record.seq_start
            if not record.payload:
                continue
            off = (record.seq_start - origin) % SEQ_SPACE
            payload, off = _trim_overlap(record.payload, off, cursor)
            if not payload:
                continue
            if off > cursor:
                if run:
                    yield Segment(bytes(run), run_start, cursor, run_ts, view=self)
                    run = bytearray()
                yield Gap(cursor, off)
            if run:
                run_ts = max(run_ts, record.timestamp)
            else:
                run_start, run_ts = off, record.timestamp
            run += payload
            cursor = off + len(payload)
        if run:
            yield Segment(bytes(run), run_start, cursor, run_ts, view=self)

    def segments(self) -> Iterator[Segment]:
        """Iterate only the stream's contiguous :class:`Segment` runs, skipping holes.

        Raises:
            ZpfError: If the stream is packet-oriented (iterate
                :meth:`datagrams` instead).

        """
        return (chunk for chunk in self.chunks() if isinstance(chunk, Segment))

    def reassembled(self) -> bytes:
        """Return the whole stream as one byte string.

        A convenience for the common gap-free case.

        Returns:
            The concatenated stream bytes.

        Raises:
            ZpfError: If the stream is packet-oriented, or has a
                :class:`Gap` (which would make the offsets of the joined
                bytes meaningless — use :meth:`chunks` to handle the hole).

        """
        parts: list[bytes] = []
        for chunk in self.chunks():
            if isinstance(chunk, Gap):
                pid = self._participant.participant_id
                msg = (
                    f"participant {pid}'s stream has a gap at [{chunk.off_start}, "
                    f"{chunk.off_end}) and cannot be fully reassembled; iterate "
                    "chunks() or segments() to handle the hole"
                )
                raise ZpfError(msg)
            parts.append(chunk.data)
        return b"".join(parts)

    def datagrams(self) -> Iterator[Datagram]:
        """Iterate the stream one record at a time, as whole datagrams.

        The natural idiom for a packet-oriented stream, but defined for any
        stream: offsets are logical when the stream is stream-oriented and
        cumulative otherwise. Every record is yielded, pure-ACK records
        included.

        Yields:
            One :class:`Datagram` per record, in stored order. A
            Discontinuity yields nothing of its own; a declared ``width``
            displaces every record after it, exactly as in
            :func:`record_ranges` — the two walk the same space and must
            not disagree.

        Note:
            Records only, so a break between two of them is **not visible
            here** — and with an unknown width the two are numerically
            adjacent, so the offsets do not betray it either. A consumer
            that must not splice across a break wants :meth:`units`.

        """
        for unit in self.units():
            if isinstance(unit, Datagram):
                yield unit

    def units(self) -> Iterator[Datagram | Break]:
        """Iterate the stream's datagrams **and** the breaks between them.

        The specification forbids a consumer from treating the records
        either side of a :class:`~zpf.blocks.Discontinuity` as contiguous.
        Honouring that needs the break to be visible, which is what this
        adds over :meth:`datagrams`: an absent ``width`` contributes 0, so
        the two records are numerically adjacent and nothing in their
        offsets says they do not join.

        The same distinction :meth:`zpf.SessionReader.stream` and
        :meth:`~zpf.SessionReader.stream_blocks` draw, one level up.

        Yields:
            Each :class:`Datagram` and :class:`Break`, in stored order.

        Example:
            >>> for unit in view.units():
            ...     if isinstance(unit, zpf.Break):
            ...         flush()  # whatever follows did not adjoin what came before
            ...     else:
            ...         feed(unit.data)

        """
        hinted = self.is_stream_oriented
        origin = self._origin() if hinted else None
        cursor = 0
        for block in self._blocks():
            if isinstance(block, Discontinuity):
                yield Break(off_start=cursor, width=block.width, reason=block.reason)
                if not hinted:
                    cursor += block.width or 0
                continue
            record = block
            if hinted:
                if record.seq_start is None:
                    continue
                if origin is None:
                    origin = record.seq_start
                off = (record.seq_start - origin) % SEQ_SPACE
            else:
                off = cursor
                cursor += len(record.payload)
            yield Datagram(
                data=record.payload,
                off_start=off,
                off_end=off + len(record.payload),
                ts=record.timestamp,
                record=record,
            )

    def cited_as(self, source: SourceHandle | int) -> StreamView:
        """Return a copy of this view that cites ``source`` by default.

        Bind the output file's ``zpf-input`` Source once, and every later
        :meth:`cite` call needs only offsets::

            stream = stream.cited_as(writer.add_source("zpf-input", uri=path))
            span = stream.cite(0, 40)

        Args:
            source: The declared Source, or its raw ``source_id``.

        Returns:
            A new view over the same stream, with the source bound.

        """
        return StreamView(self._participant, self._blocks, source=source)

    def cite(
        self, off_start: int, off_end: int, *, source: SourceHandle | int | None = None
    ) -> Span:
        """Cite a range of this stream, with the input's ids filled in.

        Saves repeating the input's ``session_id`` and ``participant_id`` on
        every span, and makes it impossible to cite the wrong stream by
        accident.

        The ``source`` is not something the input file can supply: a span's
        ``source_id`` names a Source Descriptor in the *citing* file, so it
        is the id the decoder's own writer handed back for its ``zpf-input``
        source. Pass it here, or bind it once with :meth:`cited_as`.

        Args:
            off_start: First logical stream offset to cite.
            off_end: One past the last offset to cite.
            source: The declared ``zpf-input`` Source, or its raw
                ``source_id``. Optional when bound via :meth:`cited_as`.

        Returns:
            A :class:`~zpf.blocks.Span` over ``[off_start, off_end)`` of
            this participant's stream.

        Raises:
            ZpfError: If no source was given here or bound earlier.
            EncodeError: If the range is malformed (``off_end`` before
                ``off_start``, or an offset out of range).

        """
        return Span(
            source_id=self._resolve_source(source),
            session_id=self._participant.session_id,
            participant_id=self._participant.participant_id,
            off_start=off_start,
            off_end=off_end,
        )

    def _resolve_source(self, source: SourceHandle | int | None) -> int:
        if source is not None:
            return _source_id_of(source)
        if self._source_id is not None:
            return self._source_id
        msg = (
            "cite() needs the zpf-input Source to reference: pass source=<handle from "
            "writer.add_source('zpf-input', ...)>, or bind it once with "
            "view.cited_as(handle). A span's source_id lives in the citing file's id "
            "namespace, so the file being read cannot supply it"
        )
        raise ZpfError(msg)

    def _origin(self) -> int | None:
        """Return the logical-offset origin from ``isn``, or None until a record fixes it."""
        isn = self._participant.isn
        return None if isn is None else (isn + 1) % SEQ_SPACE

    def _require_stream_oriented(self, method: str) -> None:
        if not self.is_stream_oriented:
            pid = self._participant.participant_id
            msg = (
                f"{method}() needs a stream-oriented participant (one whose records "
                f"carry seq_start); participant {pid} is packet-oriented — iterate "
                "datagrams() instead"
            )
            raise ZpfError(msg)


def _source_id_of(source: SourceHandle | int) -> int:
    """Accept a declared Source or a raw source_id, and return the id."""
    if isinstance(source, int):
        return source
    try:
        return source.source_id
    except AttributeError:
        msg = (
            "source must be a SourceHandle (from writer.add_source) or an int "
            f"source_id, not {type(source).__name__}"
        )
        raise ZpfError(msg) from None


def _trim_overlap(payload: bytes, off: int, cursor: int) -> tuple[bytes, int]:
    """Drop bytes already covered, favouring what was emitted first.

    A conformant file has no overlap; this keeps a malformed one from
    double-counting bytes rather than trusting it blindly.
    """
    if off >= cursor:
        return payload, off
    trimmed = payload[cursor - off :]
    return trimmed, cursor

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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from zpf.errors import ZpfError
from zpf.order import SEQ_SPACE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from zpf.blocks import Participant, Record


@dataclass(frozen=True)
class Segment:
    """A maximal contiguous run of reassembled stream bytes.

    Attributes:
        data: The run's bytes.
        off_start: Logical stream offset of ``data[0]``.
        off_end: One past the run's last offset (``off_start + len(data)``).
        ts: Completion time of the run — the timestamp of the last record
            that contributed to it, in the file's ``tick_hz`` ticks.

    """

    data: bytes
    off_start: int
    off_end: int
    ts: int


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
        self, participant: Participant, records: Callable[[], Iterator[Record]]
    ) -> None:
        self._participant = participant
        self._records = records

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
                    yield Segment(bytes(run), run_start, cursor, run_ts)
                    run = bytearray()
                yield Gap(cursor, off)
            if run:
                run_ts = max(run_ts, record.timestamp)
            else:
                run_start, run_ts = off, record.timestamp
            run += payload
            cursor = off + len(payload)
        if run:
            yield Segment(bytes(run), run_start, cursor, run_ts)

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
            One :class:`Datagram` per record, in stored order.

        """
        hinted = self.is_stream_oriented
        origin = self._origin() if hinted else None
        cursor = 0
        for record in self._records():
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


def _trim_overlap(payload: bytes, off: int, cursor: int) -> tuple[bytes, int]:
    """Drop bytes already covered, favouring what was emitted first.

    A conformant file has no overlap; this keeps a malformed one from
    double-counting bytes rather than trusting it blindly.
    """
    if off >= cursor:
        return payload, off
    trimmed = payload[cursor - off :]
    return trimmed, cursor

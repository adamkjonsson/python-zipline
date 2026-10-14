"""Causal ordering: RFC 1982 serial arithmetic and the streaming merge.

The format's signature feature is ordering the two directions of a TCP
session by their seq/ack **happens-before** relation instead of skew-prone
capture clocks: a segment from B carrying ``ack = N`` proves B had received
A's stream up to byte ``N`` before building it, an edge that holds
regardless of either clock. :func:`causal_merge` implements the
specification's streaming k-way merge over per-participant record streams
(already ``seq_start``-sorted, as the format guarantees), holding only one
frontier record per participant — never the quadratic edge-building form,
never a sort. :func:`verify_sequenced` is the reader-side check that a
SEQUENCED session's stored order really is a valid causal linearization.

Absolute TCP sequence numbers are 32-bit and wrap, and serial-number
arithmetic (RFC 1982) compares two of them: ``a < b`` iff
``((a - b) mod 2**32)`` has its high bit set. That is well-defined only for
values within ``2**31`` of each other. (For values exactly ``2**31`` apart it
is inherently ambiguous, and :func:`seq_lt` returns True for both orderings.)
Two *consecutive* values of one field are within that range; an ack and a
record end can be arbitrarily far apart. So since ``0.22`` neither is
compared raw (`zipline#151 <https://github.com/adamkjonsson/zipline/issues/151>`_):
both are **unwrapped** into the peer's offset space by ``_Walk``, which
steps from one value to the next by :func:`serial_delta`, and compared as
plain integers. A record's end is its offset plus its length
(``_Placer``); an ack's position is walked along its sender's records
(``_AckWalk``). Serial comparison survives only between consecutive
``seq_start`` values, for the per-participant ordering rule.

**One case the walk cannot read, and the producer's duty for it.** A step
of ``2**31`` or more is undefined. Forward, it reads as a retreat: the ack is
underestimated, which only removes edges and cannot make a correct order
fail. Backward — a segment retransmitted after 2 GiB of its peer's data and
stored ahead of an original — it reads as a jump and invents edges, so the
specification has the producer omit that retransmission's ``ack``. No writer
guard can enforce it, because a wide retreat and a wide jump are the same
bits.

Scope notes, per the specification: ack edges are defined *pairwise*, so
they are applied only to two-participant sessions; a session with any other
participant count merges by ``(timestamp, pid)`` alone, which is sound exactly
when its records share one trustworthy clock. Through ``0.18`` a file could
assert that with the SINGLE_CLOCK header flag; ``0.19`` removed it along with
the sequencing basis, so the soundness condition is unchanged and nothing in
the file states it.

Timestamps order records in exactly one place — the tie-break between
causally concurrent records during a merge. They are **not** an ordering
invariant: a reader must not reject a file, discard a session, or re-sort a
sequenced session because stored timestamps run backwards. That is an
expected consequence of skewed capture clocks and of causal sequencing, not
a corruption signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zpf.blocks import RecordFlags
from zpf.errors import SemanticError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from zpf.blocks import Record

SEQ_SPACE = 1 << 32
"""The size of the TCP sequence-number space."""

_SEQ_HIGH_BIT = 1 << 31


def seq_lt(a: int, b: int) -> bool:
    """Return whether sequence number ``a`` precedes ``b`` (serial arithmetic).

    Args:
        a: A 32-bit sequence position.
        b: A 32-bit sequence position.

    Returns:
        True iff ``((a - b) mod 2**32)`` has its high bit set — i.e. ``a``
        is behind ``b`` in the wrapped sequence space.

    """
    return a != b and (a - b) % SEQ_SPACE >= _SEQ_HIGH_BIT


def serial_delta(a: int, b: int) -> int:
    """Return the signed serial distance from ``b`` to ``a``: ``a − b`` under RFC 1982.

    The comparison :func:`seq_lt` makes, as a number. It is what the offset
    space is built from since ``0.21``: a transport record's offset is its
    predecessor's plus the serial delta of their ``seq_start`` values, which is
    well-defined at any stream length because the ordering rule keeps each
    record within 2³¹ of the one before — exactly the range in which the
    delta is defined. For two positions under 2³¹ apart it is the plain
    difference; across the 2³² wrap it is the short way round.

    Args:
        a: A 32-bit sequence position.
        b: A 32-bit sequence position.

    Returns:
        An integer in ``[−2³¹, 2³¹)``: positive iff ``b`` precedes ``a``,
        zero iff they are equal, and negative iff ``a`` precedes ``b`` —
        the same sign :func:`seq_lt` reports. A distance of exactly 2³¹ is
        the undefined case RFC 1982 names, and reads as negative here,
        which is the side the ordering rule puts it on.

    """
    delta = (a - b) % SEQ_SPACE
    return delta - SEQ_SPACE if delta >= _SEQ_HIGH_BIT else delta


def seq_leq(a: int, b: int) -> bool:
    """Return whether ``a`` equals or precedes ``b`` in the sequence space."""
    return a == b or seq_lt(a, b)


def seq_geq(a: int, b: int) -> bool:
    """Return whether ``a`` equals or follows ``b`` in the sequence space."""
    return a == b or seq_lt(b, a)


def record_end(seq_start: int, payload_len: int) -> int:
    """Return one past a record's last byte: ``seq_start + payload_len`` mod 2**32.

    The raw wire value, for display and for the per-participant ordering
    rule. It is **not** what an ``ack`` is compared against: since ``0.22``
    that is a position in the stream's offset space, and since ``0.23`` a
    zero-length record's end depends on the sequence numbers it occupies, so
    a pure ACK has none (see :func:`zpf.causal_merge`).
    """
    return (seq_start + payload_len) % SEQ_SPACE


def _sequence_end(record: Record, offset: int | None) -> int | None:
    """Return the record's end for merge step 2, or None if it has none.

    Since ``0.23`` (`zipline#160
    <https://github.com/adamkjonsson/zipline/issues/160>`_) a record's end is
    one past the last **sequence number** it occupies, because that is what an
    ``ack`` acknowledges: each byte of data, and the one number a SYN and a FIN
    each consume.

    * **Data**: ``offset + payload_len``, whether or not it carries FIN. An ack
      covering the data proves the segment was sent.
    * **Zero-length SYN**: its offset. The SYN occupies ``isn``, one below the
      origin a handshake record is stored at, so an ack of ``isn + 1`` covers
      it.
    * **Zero-length FIN**: ``offset + 1``. The FIN occupies the number at its
      offset, so an ack of that offset does not cover it.
    * **Anything else zero-length** — a pure ACK, a bare RST — has **no end**:
      it occupies no sequence number, so an ack reaching its offset says
      nothing about it, and no edge runs from it. Through ``0.22`` it ended
      at its offset, so a stored pure ACK drew edges backwards and the
      ordinary handshake, data, pure-ACK trace made a cycle.

    An unplaceable record (``offset`` None) has no end either. Either kind is
    still ordered by an ``ack`` it carries.
    """
    if offset is None:
        return None
    if record.payload:
        return offset + len(record.payload)
    if record.flags & RecordFlags.SYN:
        return offset
    if record.flags & RecordFlags.FIN:
        return offset + 1
    return None


class _Walk:
    """Unwrap a run of 32-bit sequence values along stored order.

    **The one unwrapping rule**, which every position in a sequence space
    comes from: each value's position is the previous value's plus their
    :func:`serial_delta`. That is well-defined at any length, because each
    step only needs two *consecutive* values to be within 2³¹ of each other.
    :class:`_Placer` walks a stream's ``seq_start`` values with it (offsets,
    since ``0.21``); :class:`_AckWalk` walks a participant's ``ack`` values
    with it (ack positions, since ``0.22``). The two differ only in what they
    do with a step backwards, so that decision stays with them and the
    arithmetic stays here.

    Args:
        anchor: The value the first step is measured from.
        position: The anchor's own position.

    """

    __slots__ = ("_value", "position")

    def __init__(self, anchor: int, position: int = 0) -> None:
        self._value = anchor % SEQ_SPACE
        self.position = position

    def delta(self, value: int) -> int:
        """Return the signed step from the current value to ``value``."""
        return serial_delta(value, self._value)

    def step(self, value: int, delta: int) -> int:
        """Move to ``value``, ``delta`` (from :meth:`delta`) along, and return the position."""
        self._value = value % SEQ_SPACE
        self.position += delta
        return self.position


class _Placer:
    """Walk one transport stream's records, placing each in its offset space.

    **The single definition of that question.** It was written three times
    before — in :func:`~zpf.reassembly.record_ranges`,
    :meth:`~zpf.reassembly.StreamView.chunks` and
    :meth:`~zpf.reassembly.StreamView.units` — and two of the three were
    wrong, which is
    `#63 <https://github.com/adamkjonsson/python-zipline/issues/63>`_; the
    function that replaced them measured every record against the origin,
    which was right until the stream passed 2 GiB and wrong after
    (`zipline#146 <https://github.com/adamkjonsson/zipline/issues/146>`_).
    It lives here rather than in :mod:`zpf.reassembly` since ``0.22``, because
    the causal merge and the stored-order check need it too, and this module
    is the one the other imports.

    Since ``0.21`` **offsets unwrap along stored order**: a record's offset is
    its predecessor's plus the :func:`serial_delta` of their ``seq_start``
    values, and the first record's predecessor is the **origin** — ``isn + 1``,
    or the first captured byte where the handshake was missed. The walk is
    well-defined at any length because the ordering rule keeps each record
    within 2³¹ of the one before, and for a stream under 2 GiB it yields
    ``(seq_start − origin) mod 2³²`` at every record, so nothing that size
    reads differently than it did.

    ``None`` means **unplaceable**, which the specification defines as
    covering no byte of the stream and contributing nothing to its extent.
    Two shapes reach it:

    * **No ``seq_start``** on a stream that is sequence-anchored.
      `unplaceable-no-seq-start` is its vector.
    * **A ``seq_start`` serially below the one it is measured from** — a
      negative delta in a space that has none. Below the origin, it would
      precede byte 0 (`unplaceable-below-origin`); below a predecessor, it is
      the out-of-order record the ordering rule forbids a writer to emit, and
      the two are one case seen from two sides.

    **An unplaceable record anchors nothing.** The predecessor is the last
    *placeable* record, so what follows an unplaceable one is measured past
    it, not from it — `unplaceable-below-origin`'s extent is 16, not 17.

    Args:
        origin: The stream's logical-offset origin, as a sequence position.

    """

    __slots__ = ("_walk",)

    def __init__(self, origin: int) -> None:
        self._walk = _Walk(origin)

    def place(self, record: Record) -> int | None:
        """Place the next record in stored order.

        Args:
            record: The record to place.

        Returns:
            The record's ``off_start``, or ``None`` if it is unplaceable —
            in which case the anchor does not move.

        """
        if record.seq_start is None:
            return None
        delta = self._walk.delta(record.seq_start)
        if delta < 0:
            return None
        return self._walk.step(record.seq_start, delta)


class _StreamPlacer:
    """A :class:`_Placer` whose origin is fixed by what arrives, for streaming callers.

    The origin is ``isn + 1`` where the participant declares an ``isn``, and
    otherwise the first captured byte: the first ``seq_start`` the stream
    carries. A caller that sees records one at a time only learns the second
    when that record arrives, so the placer is built then. Until it is,
    :attr:`origin` is ``None`` and every record is unplaceable — which is
    what a record with no ``seq_start`` on a stream with no ``isn`` is.

    Args:
        isn: The participant's declared ``isn``, if any.

    """

    __slots__ = ("_isn", "_placer", "origin")

    def __init__(self, isn: int | None) -> None:
        self._isn = isn
        self._placer: _Placer | None = None
        self.origin: int | None = None
        if isn is not None:
            self._start((isn + 1) % SEQ_SPACE)

    def _start(self, origin: int) -> _Placer:
        self.origin = origin
        self._placer = _Placer(origin)
        return self._placer

    def place(self, record: Record) -> int | None:
        """Place the next record in stored order; ``None`` if it is unplaceable."""
        placer = self._placer
        if placer is None:
            if record.seq_start is None:
                return None
            placer = self._start(record.seq_start)
        return placer.place(record)


class _AckWalk:
    """One participant's acks, unwrapped into its **peer's** offset space.

    Since ``0.22`` (`zipline#151
    <https://github.com/adamkjonsson/zipline/issues/151>`_) an ``ack`` is a
    *position* in the peer's stream, found the way an offset is: each ack's
    position is the previous ack's plus the signed serial delta of the two
    values, walking this participant's records in stored order and skipping
    those without an ``ack``. The first is measured from the peer's origin.
    Comparing positions with plain integers is what lets a sequenced stream
    pass 2 GiB with a peer whose only stored ack is its SYN-ACK's — the shape
    `#75 <https://github.com/adamkjonsson/python-zipline/issues/75>`_ found.

    Two differences from :class:`_Placer`, both from the specification:

    * **A step backwards is taken, not refused.** Stored order is
      ``seq_start`` order, not send order, so a retransmission can carry a
      later ack than the record stored after it.
    * **Positions are signed** (`zipline#157
      <https://github.com/adamkjonsson/zipline/issues/157>`_). In a mid-stream
      capture the first ack can lie below the peer's first captured byte; it
      acknowledges no captured byte, and every record end is above it.

    **The peer's origin is applied late.** A streaming caller may see this
    participant's acks before the peer's first record, and with no ``isn``
    that record is what fixes the origin. Nothing needs it sooner: the walk
    runs relative to the first ack, and :meth:`position` adds the first ack's
    delta from the origin once the caller has one.

    **A running maximum is equivalent where only ordering matters**
    (`zipline#155 <https://github.com/adamkjonsson/zipline/issues/155>`_):
    any edge the maximum adds to a record is already implied through this
    participant's own earlier record, which no order may move. So
    :attr:`highest` is what the stored-order check compares against.
    """

    __slots__ = ("_first", "_walk", "highest", "highest_ack")

    def __init__(self) -> None:
        self._first: int | None = None
        self._walk: _Walk | None = None
        self.highest: int | None = None
        self.highest_ack: int | None = None

    def observe(self, ack: int) -> int:
        """Walk to the next ack and return its position relative to the first."""
        if self._walk is None:
            self._first = ack % SEQ_SPACE
            self._walk = _Walk(ack)
            relative = 0
        else:
            relative = self._walk.step(ack, self._walk.delta(ack))
        if self.highest is None or relative > self.highest:
            self.highest = relative
            self.highest_ack = ack
        return relative

    def position(self, relative: int, origin: int) -> int:
        """Return a relative position (from :meth:`observe`) in the peer's offset space.

        Args:
            relative: A value :meth:`observe` returned.
            origin: The peer's origin, as a sequence position.

        Returns:
            The signed offset of the acknowledged byte boundary: every peer
            byte below it had been received.

        """
        if self._first is None:
            msg = "no ack has been observed, so there is no position to give"
            raise ValueError(msg)
        return serial_delta(self._first, origin) + relative


def _describe(record: Record) -> str:
    """Return a short label for a record in an ordering error message."""
    parts = [f"pid {record.sender_pid}", f"ts {record.timestamp}"]
    if record.seq_start is not None:
        end = record_end(record.seq_start, len(record.payload))
        parts.append(f"seq [{record.seq_start}, {end})")
    if record.ack is not None:
        parts.append(f"ack {record.ack}")
    return f"Record({', '.join(parts)})"


class _Frontier:
    """One participant's stream position in the merge.

    Holds the stream's **head** — the one record loaded and not yet emitted —
    and, since ``0.22``, where that record sits: its end in the participant's
    own offset space, and its ack's position in the peer's. Both come from
    walks that must see every record in stored order, which is why they are
    taken here, as each record is loaded, rather than when it is compared.

    Args:
        pid: The participant id.
        stream: Its records, in stored order.
        isn: Its declared ``isn``, which fixes its origin; ``None`` to take the
            first captured byte.

    """

    def __init__(self, pid: int, stream: Iterator[Record], isn: int | None) -> None:
        self.pid = pid
        self.stream = stream
        self.head: Record | None = None
        self.last_seq: int | None = None
        self.placer = _StreamPlacer(isn)
        self.acks = _AckWalk()
        #: The head's offset, or ``None`` if it is unplaceable.
        self.head_offset: int | None = None
        #: The head's end (see :func:`_sequence_end`), or ``None`` if it has none.
        self.head_end: int | None = None
        #: The head's ack, relative to this participant's first, if it has one.
        self.head_ack: int | None = None
        #: The highest end of any record already emitted from this stream.
        self.reached = 0

    def advance(self) -> None:
        """Load the next record, validating the stream's own ordering.

        Only the ``seq_start`` order is checked. Timestamps are **not** an
        ordering invariant: a stream's stamps may run backwards, and doing so
        is neither a corruption signal nor grounds to refuse the merge.
        """
        if self.head_end is not None:
            self.reached = max(self.reached, self.head_end)
        record = next(self.stream, None)
        self.head = record
        self.head_offset = None
        self.head_end = None
        self.head_ack = None
        if record is None:
            return
        if record.seq_start is not None:
            if self.last_seq is not None and not seq_leq(self.last_seq, record.seq_start):
                msg = (
                    f"participant {self.pid}: {_describe(record)} precedes the stream's "
                    f"previous seq_start ({self.last_seq}); the file violates the "
                    "stored-order guarantee"
                )
                raise SemanticError(msg)
            self.last_seq = record.seq_start
        self.head_offset = self.placer.place(record)
        self.head_end = _sequence_end(record, self.head_offset)
        if record.ack is not None:
            self.head_ack = self.acks.observe(record.ack)


def _pair_ready(frontier: _Frontier, peer: _Frontier) -> bool:
    """Return whether a frontier record's ack constraint is satisfied.

    The record is ready when every peer record it acknowledges has been
    emitted — equivalently (ends being monotone within a stream), when the
    peer's next unemitted record ends past the ack. Since ``0.22`` both sides
    are positions in the peer's offset space, compared as plain integers, so
    the answer holds at any stream length.

    A peer head with **no end** draws no edge itself, but the records behind
    it may, so the question is the lowest end any of them could have:

    * **Placed** (a pure ACK, a bare RST): every later record sits at its
      offset *x* or beyond, and every one with an end ends past it (data and a
      FIN at *x* + 1 or more; a SYN only at the origin, before any data), so
      the bound is *x* + 1. Since ``0.23`` this is the common case, and the
      looser bound below stalled the merge on it: a pure ACK at 0 held back
      the data whose ack, position 0, it does not answer.
    * **Unplaceable**: no offset to bound with, so where the stream has
      already reached.

    A peer whose origin is not known yet — no ``isn``, and nothing placed — has
    no position to compare with, so it holds nothing back.
    """
    head = frontier.head
    if head is None:
        return False
    if frontier.head_ack is None:
        return True
    if peer.head is None or peer.placer.origin is None:
        return True
    acked = frontier.acks.position(frontier.head_ack, peer.placer.origin)
    if peer.head_end is not None:
        peer_end = peer.head_end
    elif peer.head_offset is not None:
        peer_end = max(peer.reached, peer.head_offset + 1)
    else:
        peer_end = peer.reached
    return acked < peer_end


def causal_merge(
    streams: Mapping[int, Iterable[Record]],
    *,
    isns: Mapping[int, int | None] | None = None,
) -> Iterator[Record]:
    """Merge one session's per-participant record streams into causal order.

    Implements the specification's streaming k-way merge: each input stream
    must already be in stored (``seq_start``) order — which the format
    guarantees and :meth:`zpf.SessionReader.stream` provides — and only the
    frontier record of each stream is held in memory.

    For a **two-participant** session, a frontier record is released once
    every peer record it acknowledges has been emitted; genuinely
    concurrent records (no connecting ack) are ordered by
    ``(timestamp, pid)``. For any other participant count the merge is by
    ``(timestamp, pid)`` alone — the specification defines ack semantics
    only pairwise.

    Ack edges compare **positions** (`zipline#151
    <https://github.com/adamkjonsson/zipline/issues/151>`_): a record's end is
    its unwrapped offset plus its length, and an ack is unwrapped along its
    sender's records into the peer's offset space. Both are anchored at the
    peer's origin, which ``isns`` fixes at ``isn + 1``; a participant missing
    from it is anchored at its first captured byte, as a stream with no
    ``isn`` is.

    Args:
        streams: The session's record iterables, keyed by participant id.
        isns: Each participant's declared ``isn``, keyed by participant id.
            Pass the session's, as :meth:`zpf.SessionReader.timeline` does.

    Yields:
        The session's records, every record after all records that causally
        precede it.

    The merge is **stable** with respect to stored order: step 1 takes each
    participant's records in file order and nothing later disturbs it, so the
    tie-break only ever chooses *between* participants. A timestamp that runs
    backwards within one participant therefore changes nothing. Because
    ``participant_id`` is unique within its session, ``(timestamp, pid)`` is a
    total order over the frontiers, so every reader of the same file computes
    the same interleaving.

    Raises:
        SemanticError: If the merge stalls (two frontier records each
            acknowledging bytes the other has not sent — a broken file), or
            if a stream violates its stored-order guarantee.

    """
    known = isns or {}
    frontiers = [
        _Frontier(pid, iter(stream), known.get(pid)) for pid, stream in sorted(streams.items())
    ]
    for frontier in frontiers:
        frontier.advance()
    pairwise = len(frontiers) == 2
    while True:
        if pairwise:
            pairs = zip(frontiers, reversed(frontiers), strict=True)
            ready = [f for f, peer in pairs if _pair_ready(f, peer)]
            if not ready and any(f.head is not None for f in frontiers):
                first, second = (f.head for f in frontiers)
                msg = (
                    f"causal merge stalled: {_describe(first)} and {_describe(second)} "
                    "each acknowledge bytes the other has not yet sent; the file's "
                    "ordering hints are inconsistent"
                )
                raise SemanticError(msg)
        else:
            ready = [f for f in frontiers if f.head is not None]
        if not ready:
            return
        best = min(ready, key=_merge_key)
        record = best.head
        if record is None:  # unreachable: readiness guarantees a head
            continue
        best.advance()
        yield record


def _merge_key(frontier: _Frontier) -> tuple[int, int]:
    """Order ready frontiers by (timestamp, pid) — the concurrent tie-break."""
    head = frontier.head
    return (head.timestamp if head is not None else 0, frontier.pid)


class _StoredOrder:
    """Incremental state for a SEQUENCED session's stored-order rules.

    One implementation of the rules, driven from both sides: reader-side by
    :func:`verify_sequenced` over a session already on disk, writer-side by
    :class:`~zpf.writer.SessionWriter` one record at a time as they are
    emitted. Single pass, O(1) state per participant, and it never buffers
    a record — which is what lets the writer run it without changing the
    streaming shape of the write path.

    The ack rule compares **positions** since ``0.22``: a record's end in its
    sender's offset space against the highest ack position any peer record
    stored before it carried. A running maximum is enough for that
    (`zipline#155 <https://github.com/adamkjonsson/zipline/issues/155>`_).

    Args:
        participant_count: The session's participant count, if known. Ack
            cross-checks apply when it is 2; with ``None`` they apply while
            at most two senders have appeared and are dropped from the
            first record of a third sender on. A writer never knows the
            count up front — participants are declared on first use — so
            ``None`` is the writer's case, and its behaviour is exactly
            what that needs.
        isns: Each participant's declared ``isn``, keyed by participant id.
            Read at a participant's first record, so a writer may pass the
            mapping it fills as participants are declared.

    """

    def __init__(
        self,
        participant_count: int | None = None,
        isns: Mapping[int, int | None] | None = None,
    ) -> None:
        self._last_seq: dict[int, int] = {}
        self._isns: Mapping[int, int | None] = isns if isns is not None else {}
        self._placers: dict[int, _StreamPlacer] = {}
        self._acks: dict[int, _AckWalk] = {}
        self._senders: set[int] = set()
        self._count = participant_count
        self._ack_checks = participant_count == 2 or participant_count is None

    def observe(self, record: Record) -> None:
        """Fold one record in, raising if it breaks the stored order.

        Raises:
            SemanticError: If this record precedes its participant's
                previous ``seq_start``, or appears after a peer record that
                already acknowledged its bytes.

        """
        pid = record.sender_pid
        self._senders.add(pid)
        if self._count is None and len(self._senders) > 2:
            self._ack_checks = False
        if record.seq_start is not None:
            previous = self._last_seq.get(pid)
            if previous is not None and not seq_leq(previous, record.seq_start):
                msg = (
                    f"{_describe(record)} precedes participant {pid}'s previous "
                    f"seq_start ({previous}); the stored order violates the "
                    "per-participant guarantee"
                )
                raise SemanticError(msg)
            self._last_seq[pid] = record.seq_start
        placer = self._placers.get(pid)
        if placer is None:
            placer = self._placers[pid] = _StreamPlacer(self._isns.get(pid))
        end = _sequence_end(record, placer.place(record))
        if self._ack_checks and end is not None and placer.origin is not None:
            self._check_not_already_acked(record, end, placer.origin)
        if self._ack_checks and record.ack is not None:
            walk = self._acks.get(pid)
            if walk is None:
                walk = self._acks[pid] = _AckWalk()
            walk.observe(record.ack)

    def _check_not_already_acked(self, record: Record, end: int, origin: int) -> None:
        """Raise if an earlier peer record already acknowledged this record's bytes.

        A record with no end never reaches here — a pure ACK, a bare RST, or
        an unplaceable record — since no edge runs from it. Its own ``ack`` is
        still walked by :meth:`observe`, so it is placed after the peer bytes
        that ack covers (`zipline#157
        <https://github.com/adamkjonsson/zipline/issues/157>`_).
        """
        for other_pid, walk in self._acks.items():
            if other_pid == record.sender_pid or walk.highest is None:
                continue
            acked = walk.position(walk.highest, origin)
            if end <= acked:
                msg = (
                    f"{_describe(record)} appears after a record from participant "
                    f"{other_pid} that already acknowledged its bytes (ack "
                    f"{walk.highest_ack}, offset {acked} of this stream, against the "
                    f"record's end at {end}); the stored order is not a causal "
                    "linearization"
                )
                raise SemanticError(msg)


def verify_sequenced(
    records: Iterable[Record],
    *,
    participant_count: int | None = None,
    isns: Mapping[int, int | None] | None = None,
) -> None:
    """Check that a stored record order is a valid causal linearization.

    This is the reader-side verification of what a session's SEQUENCED flag
    asserts. Single pass, O(1) state per participant:

    * each participant's ``seq_start`` values must be non-decreasing;
    * (two-participant sessions) a record must not appear after a peer
      record that already acknowledged its bytes.

    The second rule compares positions, not raw sequence numbers (`zipline#151
    <https://github.com/adamkjonsson/zipline/issues/151>`_): the record's end
    is its unwrapped offset plus its length, and each ack is unwrapped along
    its sender's records into the peer's offset space. So a stream past 2 GiB
    checks whether or not its peer's acks kept up.

    Timestamps are deliberately not checked. They are not an ordering
    invariant in any session, sequenced or not: a stored order may run
    backwards in time, and a hint-less session's order may rest on a protocol
    sequence or an out-of-band record, neither of which the clock reflects.
    ``sequenced-session`` is the vector for exactly this — its response is
    stored five ticks *before* the request that caused it. The only
    stored-order guarantee a reader may act on is the per-participant
    ``seq_start`` rule above, which is a sequence rule, not a time rule.

    Args:
        records: The session's records, in stored order.
        participant_count: The session's participant count, if known. Ack
            cross-checks apply when it is 2; with ``None`` they apply while
            at most two senders have appeared and are dropped from the
            first record of a third sender on (the specification defines
            ack semantics only pairwise).
        isns: Each participant's declared ``isn``, keyed by participant id,
            which anchors its offsets at ``isn + 1``. A participant missing
            from it is anchored at its first captured byte. Pass the
            session's, as :meth:`zpf.SessionReader.verify` does.

    Raises:
        SemanticError: On the first violation, naming the records involved.

    """
    state = _StoredOrder(participant_count, isns)
    for record in records:
        state.observe(record)

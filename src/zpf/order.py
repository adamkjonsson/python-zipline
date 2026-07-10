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

Because absolute TCP sequence numbers are 32-bit and wrap, every comparison
uses serial-number arithmetic (RFC 1982): ``a < b`` iff
``((a - b) mod 2**32)`` has its high bit set — well-defined for values
within ``2**31`` of each other, vastly more than any in-flight window. (For
values exactly ``2**31`` apart the comparison is inherently ambiguous and
this implementation returns True for both orderings.)

Scope notes, per the specification: ack edges are defined *pairwise*, so
they are applied only to two-participant sessions; a session with any other
participant count merges by ``(timestamp, pid)`` alone, which is sound
exactly when its records share one trustworthy clock (the SINGLE_CLOCK
discussion in the spec).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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


def seq_leq(a: int, b: int) -> bool:
    """Return whether ``a`` equals or precedes ``b`` in the sequence space."""
    return a == b or seq_lt(a, b)


def seq_geq(a: int, b: int) -> bool:
    """Return whether ``a`` equals or follows ``b`` in the sequence space."""
    return a == b or seq_lt(b, a)


def record_end(seq_start: int, payload_len: int) -> int:
    """Return one past a record's last byte: ``seq_start + payload_len`` mod 2**32.

    This is the value a peer's cumulative ``ack`` is compared against; it is
    never stored in a file. A zero-length record's end is its ``seq_start``.
    """
    return (seq_start + payload_len) % SEQ_SPACE


def _describe(record: Record) -> str:
    """Return a short label for a record in an ordering error message."""
    parts = [f"pid {record.sender_pid}", f"ts {record.timestamp}"]
    if record.seq_start is not None:
        end = record_end(record.seq_start, len(record.payload))
        parts.append(f"seq [{record.seq_start}, {end})")
    if record.ack is not None:
        parts.append(f"ack {record.ack}")
    return f"Record({', '.join(parts)})"


@dataclass
class _Frontier:
    """One participant's stream position in the merge."""

    pid: int
    stream: Iterator[Record]
    head: Record | None = None
    last_seq: int | None = None
    last_hintless_ts: int | None = None

    def advance(self) -> None:
        """Load the next record, validating the stream's own ordering."""
        record = next(self.stream, None)
        self.head = record
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
        else:
            if self.last_hintless_ts is not None and record.timestamp < self.last_hintless_ts:
                msg = (
                    f"participant {self.pid}: {_describe(record)} steps backwards in time "
                    f"(previous timestamp {self.last_hintless_ts}); hint-less streams can "
                    "only be merged when their timestamps are non-decreasing — sort the "
                    "session's records() by timestamp yourself if the data is small"
                )
                raise SemanticError(msg)
            self.last_hintless_ts = record.timestamp


def _pair_ready(frontier: _Frontier, peer: _Frontier) -> bool:
    """Return whether a frontier record's ack constraint is satisfied.

    The record is ready when every peer record it acknowledges has been
    emitted — equivalently (ends being monotone within a stream), when the
    peer's next unemitted record ends past the ack.
    """
    head = frontier.head
    if head is None:
        return False
    if head.ack is None:
        return True
    peer_head = peer.head
    if peer_head is None or peer_head.seq_start is None:
        return True
    return seq_lt(head.ack, record_end(peer_head.seq_start, len(peer_head.payload)))


def causal_merge(streams: Mapping[int, Iterable[Record]]) -> Iterator[Record]:
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

    Args:
        streams: The session's record iterables, keyed by participant id.

    Yields:
        The session's records, every record after all records that causally
        precede it.

    Raises:
        SemanticError: If the merge stalls (two frontier records each
            acknowledging bytes the other has not sent — a broken file), if
            a stream violates its stored-order guarantee, or if a hint-less
            stream's timestamps step backwards.

    """
    frontiers = [_Frontier(pid, iter(stream)) for pid, stream in sorted(streams.items())]
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


def verify_sequenced(
    records: Iterable[Record], *, participant_count: int | None = None
) -> None:
    """Check that a stored record order is a valid causal linearization.

    This is the reader-side verification of what a session's SEQUENCED flag
    asserts. Single pass, O(1) state per participant:

    * each participant's ``seq_start`` values must be non-decreasing;
    * (two-participant sessions) a record must not appear after a peer
      record that already acknowledged its bytes;
    * hint-less records must not step backwards in time (their sequenced
      order *is* the timestamp order).

    Args:
        records: The session's records, in stored order.
        participant_count: The session's participant count, if known. Ack
            cross-checks apply when it is 2; with ``None`` they apply while
            at most two senders have appeared and are dropped from the
            first record of a third sender on (the specification defines
            ack semantics only pairwise).

    Raises:
        SemanticError: On the first violation, naming the records involved.

    """
    last_seq: dict[int, int] = {}
    max_ack: dict[int, int] = {}
    senders: set[int] = set()
    last_hintless_ts: int | None = None
    ack_checks = participant_count == 2 or participant_count is None
    for record in records:
        pid = record.sender_pid
        senders.add(pid)
        if participant_count is None and len(senders) > 2:
            ack_checks = False
        if record.seq_start is not None:
            previous = last_seq.get(pid)
            if previous is not None and not seq_leq(previous, record.seq_start):
                msg = (
                    f"{_describe(record)} precedes participant {pid}'s previous "
                    f"seq_start ({previous}); the stored order violates the "
                    "per-participant guarantee"
                )
                raise SemanticError(msg)
            last_seq[pid] = record.seq_start
            if ack_checks:
                end = record_end(record.seq_start, len(record.payload))
                _check_not_already_acked(record, end, max_ack)
        elif last_hintless_ts is not None and record.timestamp < last_hintless_ts:
            msg = (
                f"{_describe(record)} steps backwards in time (previous hint-less "
                f"timestamp {last_hintless_ts}); a hint-less sequenced order is "
                "the timestamp order"
            )
            raise SemanticError(msg)
        else:
            last_hintless_ts = record.timestamp
        if ack_checks and record.ack is not None:
            current = max_ack.get(pid)
            if current is None or seq_lt(current, record.ack):
                max_ack[pid] = record.ack


def _check_not_already_acked(record: Record, end: int, max_ack: dict[int, int]) -> None:
    """Raise if an earlier peer record already acknowledged this record's bytes."""
    for other_pid, acked in max_ack.items():
        if other_pid != record.sender_pid and seq_geq(acked, end):
            msg = (
                f"{_describe(record)} appears after a record from participant "
                f"{other_pid} that already acknowledged its bytes (ack {acked}); "
                "the stored order is not a causal linearization"
            )
            raise SemanticError(msg)

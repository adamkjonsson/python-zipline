"""Half-open integer-interval helpers shared by the coverage code.

Both the after-the-fact coverage check (:func:`zpf.check_coverage`) and the
decode-stage auto-fill (:class:`zpf.DecodeStage`) reason about the same
thing: which offsets of an input stream a set of ranges does and does not
cover. Keeping the arithmetic in one place stops the two from drifting.

All intervals are ``(start, end)`` pairs with ``start < end`` and are
compared on the half-open range ``[start, end)``.

Both ledgers hold their ranges **coalesced**: sorted, disjoint, and with
touching ranges fused, maintained by :func:`add` as each range arrives. The
alternative — appending one entry per span and sorting at the end — is what
`#96 <https://github.com/adamkjonsson/python-zipline/issues/96>`_ was filed
about: it made a derived file's write grow with its record count, which the
format's goals promise it does not. Coalescing is sound here because nothing
asks either ledger *which* range covered an offset, only whether one did.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from operator import itemgetter

_START = itemgetter(0)
_END = itemgetter(1)


def add(into: list[tuple[int, int]], start: int, end: int) -> None:
    """Merge ``[start, end)`` into a coalesced interval list, in place.

    ``into`` must already be coalesced — sorted, disjoint, no two ranges
    touching — and is left that way. An empty or reversed range is ignored,
    so a caller need not pre-filter one.

    Ranges that *touch* are fused: adding ``[4, 8)`` to ``[[0, 4)]`` gives
    ``[[0, 8)]``, not two entries. That is what keeps the list at one entry
    for the ordinary case of a stage citing its input in order, and it loses
    nothing — the two spellings cover the same offsets, and no caller asks
    which range covered one.

    Args:
        into: The coalesced list to merge into, modified in place.
        start: First offset of the range to add.
        end: One past its last offset.

    """
    if start >= end:
        return
    if into:
        # The hot path: a stage citing its input in increasing order, where
        # every range extends or follows the last. Keeping it off the
        # bisect below is what makes this O(1) per span in the common case.
        last_start, last_end = into[-1]
        if start >= last_start:
            if start <= last_end:
                if end > last_end:
                    into[-1] = (last_start, end)
            else:
                into.append((start, end))
            return
    else:
        into.append((start, end))
        return
    # Out of order: find every range this one touches and fuse them.
    # `into` is sorted by start and disjoint, so its ends are sorted too.
    lo = bisect_left(into, start, key=_END)
    hi = bisect_right(into, end, key=_START)
    if lo == hi:
        into.insert(lo, (start, end))
    else:
        into[lo:hi] = [(min(start, into[lo][0]), max(end, into[hi - 1][1]))]


def coalesce(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return ``intervals`` as a sorted, disjoint list with touching ranges fused.

    The batch form of :func:`add`, for a list that was gathered without it.
    Empty and reversed ranges are dropped.

    Args:
        intervals: ``(start, end)`` pairs, in any order, overlaps allowed.

    Returns:
        The same covered offsets, as few ranges as possible.

    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        add(merged, start, end)
    return merged


def complement(intervals: list[tuple[int, int]], extent: int) -> list[tuple[int, int]]:
    """Return the sub-ranges of ``[0, extent)`` not covered by ``intervals``.

    Args:
        intervals: Sorted ``(start, end)`` pairs (overlaps allowed).
        extent: The upper bound of the space being covered.

    Returns:
        The uncovered sub-ranges, in order.

    """
    gaps: list[tuple[int, int]] = []
    position = 0
    for start, end in intervals:  # sorted
        if start > position:
            gaps.append((position, min(start, extent)))
        position = max(position, end)
        if position >= extent:
            break
    if position < extent:
        gaps.append((position, extent))
    return [(start, end) for start, end in gaps if start < end]


def intersections(
    first: list[tuple[int, int]], second: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return the pairwise intersections of two sorted interval lists.

    Args:
        first: Sorted ``(start, end)`` pairs.
        second: Sorted ``(start, end)`` pairs.

    Returns:
        Every non-empty overlap between an interval of each list, in order.

    """
    result: list[tuple[int, int]] = []
    i = j = 0
    while i < len(first) and j < len(second):
        start = max(first[i][0], second[j][0])
        end = min(first[i][1], second[j][1])
        if start < end:
            result.append((start, end))
        if first[i][1] <= second[j][1]:
            i += 1
        else:
            j += 1
    return result

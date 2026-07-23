"""Half-open integer-interval helpers shared by the coverage code.

Both the after-the-fact coverage check (:func:`zpf.check_coverage`) and the
decode-stage auto-fill (:class:`zpf.DecodeStage`) reason about the same
thing: which offsets of an input stream a set of ranges does and does not
cover. Keeping the arithmetic in one place stops the two from drifting.

All intervals are ``(start, end)`` pairs with ``start < end`` and are
compared on the half-open range ``[start, end)``.
"""

from __future__ import annotations


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

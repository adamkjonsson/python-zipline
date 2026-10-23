"""Tests for the shared half-open interval arithmetic."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from zpf._intervals import add, coalesce, complement, intersections

# Small coordinates, so a range can be compared as the set of offsets it covers.
RANGES = st.lists(
    st.tuples(st.integers(0, 40), st.integers(0, 40)),
    max_size=24,
)
#: Ranges as the ledgers actually hold them. Both filter ``start < end`` before
#: inserting, so a reversed range never reaches one — and `complement` does not
#: tolerate one, where `coalesce` drops it. Where a property compares the two,
#: it compares them on the input they both really see.
WELL_FORMED = st.lists(
    st.tuples(st.integers(0, 40), st.integers(1, 8)).map(lambda p: (p[0], p[0] + p[1])),
    max_size=24,
)


def offsets(intervals: list[tuple[int, int]]) -> set[int]:
    """Return the offsets a list of ranges covers, however it is spelled."""
    return {k for start, end in intervals for k in range(start, end)}


def is_coalesced(intervals: list[tuple[int, int]]) -> bool:
    """Sorted, non-empty ranges, and no two of them touching or overlapping."""
    if any(start >= end for start, end in intervals):
        return False
    return all(a[1] < b[0] for a, b in zip(intervals, intervals[1:], strict=False))


def test_add_fuses_a_touching_range():
    """[0, 4) and [4, 8) are one range, not two — that is what bounds the list."""
    held: list[tuple[int, int]] = []
    add(held, 0, 4)
    add(held, 4, 8)
    assert held == [(0, 8)]


def test_add_in_order_stays_one_range():
    """A stage citing its input in order holds one entry, whatever the count."""
    held: list[tuple[int, int]] = []
    for k in range(500):
        add(held, k * 64, (k + 1) * 64)
    assert held == [(0, 32_000)]


def test_add_keeps_a_real_gap_apart():
    held: list[tuple[int, int]] = []
    add(held, 0, 4)
    add(held, 8, 12)
    assert held == [(0, 4), (8, 12)]


def test_add_bridges_a_gap_when_the_middle_arrives():
    held: list[tuple[int, int]] = []
    add(held, 0, 4)
    add(held, 8, 12)
    add(held, 4, 8)
    assert held == [(0, 12)]


def test_add_absorbs_a_range_already_covered():
    held = [(0, 100)]
    add(held, 10, 20)
    assert held == [(0, 100)]


def test_add_accepts_ranges_out_of_order():
    held: list[tuple[int, int]] = []
    for start, end in ((20, 24), (0, 4), (12, 16), (4, 8)):
        add(held, start, end)
    assert held == [(0, 8), (12, 16), (20, 24)]


def test_add_ignores_an_empty_or_reversed_range():
    """A caller need not pre-filter one, and a zero-width span covers nothing."""
    held: list[tuple[int, int]] = []
    add(held, 5, 5)
    add(held, 9, 4)
    assert held == []


def test_coalesce_drops_nothing_and_invents_nothing():
    assert coalesce([(8, 12), (0, 4), (2, 9)]) == [(0, 12)]


@given(RANGES)
def test_coalesce_preserves_the_offsets_and_normalises_the_spelling(
    ranges: list[tuple[int, int]],
):
    merged = coalesce(ranges)
    assert is_coalesced(merged)
    assert offsets(merged) == offsets(ranges)


@given(RANGES)
def test_adding_one_at_a_time_matches_coalescing_the_batch(ranges: list[tuple[int, int]]):
    """Incremental and batch must agree, or the ledgers drift from each other."""
    held: list[tuple[int, int]] = []
    for start, end in ranges:
        add(held, start, end)
        assert is_coalesced(held)  # the invariant holds at every step
    assert held == coalesce(ranges)


@given(WELL_FORMED, st.integers(0, 40))
def test_complement_does_not_see_the_spelling(
    ranges: list[tuple[int, int]], extent: int
):
    """What coalescing must not change: which offsets are reported uncovered."""
    assert complement(sorted(ranges), extent) == complement(coalesce(ranges), extent)


@given(WELL_FORMED, WELL_FORMED)
def test_intersections_covers_the_same_offsets_either_way(
    first: list[tuple[int, int]], second: list[tuple[int, int]]
):
    """Coalescing each side separately keeps the overlap — the clash auto-fill catches.

    The *spelling* may differ: merged inputs can report one wider overlap
    where unmerged ones reported two adjacent ones. The offsets may not.
    """
    loose = intersections(sorted(first), sorted(second))
    tight = intersections(coalesce(first), coalesce(second))
    assert offsets(loose) == offsets(tight)
    assert bool(loose) == bool(tight)  # and auto-fill's raise-or-not cannot move

"""The bounded-memory guarantees, as assertions rather than a benchmark.

The format's goals promise a writer can *flush a finished session and forget
it, keeping memory bounded on unbounded input*. Through `0.6.0` that held for
a capture-sourced file and not for a derived one: the checker's coverage
ledger kept one interval per span and coalesced none of them
(`#96 <https://github.com/adamkjonsson/python-zipline/issues/96>`_), so a
decode stage's write grew with its record count. The suite could not see it —
every vector is a handful of blocks, and peak memory is not something a
round-trip test measures.

These tests are written to be **stable, not precise**. They assert that peak
memory does not scale with the record count, and that the ledger holds the one
range it should; they deliberately do not assert a byte figure, which would
vary with the interpreter and fail for the wrong reason.
"""

from __future__ import annotations

import tracemalloc
from typing import TYPE_CHECKING

import zpf

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PAYLOAD = b"x" * 64
#: Enough records that a per-record leak dwarfs the fixed cost, small enough
#: to keep the suite quick. The ratio between the two is what is asserted.
FEW = 1_000
MANY = 16_000


def write_decode_stage_shaped(path: Path, count: int) -> zpf.FileWriter:
    """Write a derived file whose records cite their input in order.

    The ordinary decode-stage shape, and the one that should cost a single
    coalesced range per input stream however many records it holds.
    """
    with zpf.create(path, tick_hz=1_000_000, produced_by="m 1", produced_at=1) as writer:
        source = writer.add_source("zpf-input", uri="in.zpf", digest="sha256:00")
        decoder = writer.add_decoder("d", version="1")
        session = writer.begin_session(proto="http", key="a <-> b", session_id=1)
        sender = session.participant("10.0.0.1:1")
        for k in range(count):
            session.record(
                sender, ts=k, payload=PAYLOAD, source=source,
                decoded=zpf.Decoded(
                    decoder=decoder,
                    spans=[zpf.Span(
                        source_id=source.source_id, session_id=1, participant_id=0,
                        off_start=k * 64, off_end=(k + 1) * 64,
                    )],
                ),
            )
        session.end(
            reason="capture-end",
            input_extents=[zpf.InputExtent(
                source_id=source.source_id, session_id=1, participant_id=0,
                extent=count * 64,
            )],
        )
    return writer


def peak_of(fn: Callable[..., object], *args: object) -> int:
    tracemalloc.start()
    try:
        fn(*args)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_a_derived_write_does_not_grow_with_its_record_count(tmp_path: Path):
    """#96: sixteen times the records must not cost sixteen times the memory."""
    few = peak_of(write_decode_stage_shaped, tmp_path / "few.zpf", FEW)
    many = peak_of(write_decode_stage_shaped, tmp_path / "many.zpf", MANY)
    # Before #96 this ratio tracked the record count (~16x). The bound is
    # loose on purpose: what matters is that it is a constant, not which one.
    assert many < few * 2, (
        f"peak memory grew from {few} to {many} bytes for {MANY // FEW}x the "
        f"records; a derived write is meant to be bounded-memory"
    )


def test_the_coverage_ledger_holds_one_range_for_a_stream_cited_in_order(tmp_path: Path):
    """The invariant behind the test above, stated where it cannot drift."""
    writer = write_decode_stage_shaped(tmp_path / "one.zpf", FEW)
    streams = writer._checker._coverage._streams
    (key,) = streams  # one input stream was cited, so the ledger holds one
    assert streams[key].spans == [(0, FEW * 64)]
    assert streams[key].undecoded == []


def test_a_derived_read_does_not_hold_a_range_per_span(tmp_path: Path):
    """The same ledger runs on the read side, so the saving is the reader's too."""
    def read(path: Path) -> int:
        with zpf.open(path) as reader:
            return reader.session(1).record_count

    write_decode_stage_shaped(tmp_path / "few.zpf", FEW)
    write_decode_stage_shaped(tmp_path / "many.zpf", MANY)
    few = peak_of(read, tmp_path / "few.zpf")
    many = peak_of(read, tmp_path / "many.zpf")
    # The reader's index is eager in *structure* by design — one locator per
    # record, twice — so this ratio is not expected to be flat. It is
    # expected to be the index alone: ~50 B/record, not the ~200 B/record the
    # span ledger added on top.
    per_record = (many - few) / (MANY - FEW)
    assert per_record < 120, (
        f"{per_record:.0f} bytes retained per record on open; the structural "
        f"index alone should be well under that"
    )

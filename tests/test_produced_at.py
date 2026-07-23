"""Tests for datetime produced_at and the public unix_seconds helper."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta, timezone

import pytest

import zpf

# 2023-11-14 22:13:20 UTC
AWARE = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
EPOCH_SECONDS = 1_700_000_000


def header_of(data: bytes) -> zpf.FileHeader:
    with zpf.open(io.BytesIO(data)) as reader:
        return reader.header


# --- unix_seconds ---------------------------------------------------------------------


def test_unix_seconds_converts_an_aware_datetime():
    assert zpf.unix_seconds(AWARE) == EPOCH_SECONDS


def test_unix_seconds_passes_an_int_through():
    assert zpf.unix_seconds(EPOCH_SECONDS) == EPOCH_SECONDS


def test_unix_seconds_is_timezone_correct():
    # The same instant expressed in a +02:00 zone yields the same Unix second.
    shifted = AWARE.astimezone(timezone(timedelta(hours=2)))
    assert zpf.unix_seconds(shifted) == EPOCH_SECONDS


def test_unix_seconds_truncates_sub_second():
    assert zpf.unix_seconds(AWARE.replace(microsecond=999_999)) == EPOCH_SECONDS


def test_unix_seconds_rejects_a_naive_datetime():
    with pytest.raises(zpf.ZpfError, match="timezone-aware"):
        zpf.unix_seconds(datetime(2023, 11, 14, 22, 13, 20))


# --- produced_at on create() ----------------------------------------------------------


def test_create_accepts_a_datetime_produced_at():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1, produced_by="t 1.0", produced_at=AWARE):
        pass
    assert header_of(sink.getvalue()).produced_at == EPOCH_SECONDS


def test_create_still_accepts_an_int_produced_at():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1, produced_by="t 1.0", produced_at=EPOCH_SECONDS):
        pass
    assert header_of(sink.getvalue()).produced_at == EPOCH_SECONDS


def test_create_leaves_produced_at_unset_when_omitted():
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1):
        pass
    assert header_of(sink.getvalue()).produced_at is None


def test_create_rejects_a_naive_produced_at():
    with pytest.raises(zpf.ZpfError, match="timezone-aware"):
        zpf.create(io.BytesIO(), tick_hz=1, produced_by="t", produced_at=datetime(2023, 1, 1))


# --- produced_at on decode_stage() ----------------------------------------------------


def test_decode_stage_accepts_a_datetime_produced_at():
    raw = io.BytesIO()
    with zpf.create(raw, tick_hz=1_000_000, time_epoch=0) as writer:
        writer.add_source("capture")
        with writer.begin_session(proto="tcp", session_id=7) as session:
            session.record(session.participant("a", isn=1000), ts=1, payload=b"x", seq_start=1001)

    sink = io.BytesIO()
    with zpf.decode_stage(
        io.BytesIO(raw.getvalue()),
        sink,
        decoder="http/1.1",
        produced_by="d 1.0",
        produced_at=AWARE,
    ):
        pass
    assert header_of(sink.getvalue()).produced_at == EPOCH_SECONDS

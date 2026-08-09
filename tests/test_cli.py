"""Tests for the zpf command-line tool (driving main() directly)."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from test_golden import GOLDEN
from test_transform import write_raw, write_side_a, write_side_b

import zpf
from zpf.cli import main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def golden_path(tmp_path: Path) -> Path:
    path = tmp_path / "golden.zpf"
    path.write_bytes(GOLDEN)
    return path


def test_info(golden_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["info", str(golden_path)]) == 0
    out = capsys.readouterr().out
    assert "face:      binary" in out
    assert "stream 0: capture transport" in out
    assert "source 1: capture sideA.pcap" in out
    assert "session 7: proto=tcp" in out
    assert "participants=[10.0.0.1:51000] records=1" in out


def test_cat_matches_the_converter(golden_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["cat", str(golden_path)]) == 0
    out = capsys.readouterr().out
    expected = io.StringIO()
    zpf.binary_to_jsonl(str(golden_path), expected)
    assert out == expected.getvalue()


def test_convert_round_trips(golden_path: Path, tmp_path: Path):
    jsonl_path = tmp_path / "golden.zpf.jsonl"
    binary_path = tmp_path / "back.zpf"
    assert main(["convert", str(golden_path), str(jsonl_path)]) == 0
    assert zpf.detect_face(jsonl_path) == "jsonl"
    assert main(["convert", str(jsonl_path), str(binary_path)]) == 0
    assert binary_path.read_bytes() == GOLDEN  # canonical in, canonical out


def test_validate_clean_file(golden_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["validate", str(golden_path)]) == 0
    assert "OK" in capsys.readouterr().err


def test_validate_reports_nonconformance(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "bad.zpf"
    with zpf.BlockWriter(path) as writer:  # permissive flat writer
        writer.write(zpf.FileHeader(tick_hz=1))
        writer.write(zpf.SessionEnd(session_id=9))  # never declared
    assert main(["validate", str(path)]) == 1
    captured = capsys.readouterr()
    assert "nonconformant" in captured.out
    assert "undeclared session 9" in captured.out
    assert "1 finding(s)" in captured.err


def test_validate_garbage_is_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = tmp_path / "junk.bin"
    path.write_bytes(b"\x00" * 64)
    assert main(["validate", str(path)]) == 2
    assert capsys.readouterr().err.startswith("zpf: ")


def test_validate_verify_catches_a_bad_sequenced_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = tmp_path / "badseq.zpf"
    with zpf.BlockWriter(path) as writer:
        writer.write(zpf.FileHeader(tick_hz=1))
        writer.write(zpf.Source(source_id=0, kind=zpf.SourceKind.CAPTURE))
        writer.write(
            zpf.Session(session_id=0, proto="tcp", flags=zpf.SessionFlags.SEQUENCED)
        )
        writer.write(zpf.Participant(session_id=0, participant_id=0, isn=1000))
        writer.write(zpf.Participant(session_id=0, participant_id=1, isn=5000))
        # Stored response-first although it acks the request: not causal.
        writer.write(
            zpf.Record(session_id=0, sender_pid=1, source_id=0, timestamp=995,
                       payload=b"resp", seq_start=5001, ack=1019)
        )
        writer.write(
            zpf.Record(session_id=0, sender_pid=0, source_id=0, timestamp=1000,
                       payload=b"x" * 18, seq_start=1001, ack=5001)
        )
    assert main(["validate", str(path)]) == 0  # single-pass checks can't see it
    assert main(["validate", "--verify", str(path)]) == 1
    assert "sequenced-order" in capsys.readouterr().out


def test_validate_input_runs_coverage(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    raw = tmp_path / "raw.zpf"
    write_raw(raw)
    from test_transform import write_decoded

    decoded = tmp_path / "decoded.zpf"
    write_decoded(decoded, raw, p1_spans=[(0, 100)], p1_undecoded=[(110, 139)])  # gap
    assert main(["validate", "--input", str(raw), str(decoded)]) == 1
    assert "coverage-gap" in capsys.readouterr().out


def test_merge_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    side_a, side_b = tmp_path / "a.zpf", tmp_path / "b.zpf"
    write_side_a(side_a)
    write_side_b(side_b)
    merged = tmp_path / "merged.zpf"
    assert main(["merge", str(side_a), str(side_b), "-o", str(merged)]) == 0
    assert main(["validate", "--verify", str(merged)]) == 0
    capsys.readouterr()
    with zpf.open(merged) as f:
        assert f.stream_kind(f.sessions()[0].session_id, 0) == (
            zpf.SourceKind.ZPF_INPUT,
            zpf.OutputLayer.TRANSPORT,
        )
        assert f.header.produced_by == f"zpf {zpf.__version__}"


def test_merge_error_is_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    side_a = tmp_path / "a.zpf"
    write_side_a(side_a)
    assert main(["merge", str(side_a), str(tmp_path / "missing.zpf"),
                 "-o", str(tmp_path / "out.zpf")]) == 2
    assert capsys.readouterr().err.startswith("zpf: ")


def test_version(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert f"zpf {zpf.__version__}" in capsys.readouterr().out

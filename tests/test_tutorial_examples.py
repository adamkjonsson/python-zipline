"""The tutorial's example scripts, run for real so they can't go stale.

Each script in ``docs/user/examples/`` is included verbatim into
``docs/user/tutorial.md`` via ``{literalinclude}``. Running them here is
what lets the docs claim the examples work: if the API changes underneath
them, this test fails before the docs quietly go wrong.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

EXAMPLES_DIR = Path(__file__).parent.parent / "docs" / "user" / "examples"


def _run(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / script)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("scripts", "expected"),
    [
        (("01_write_first_file.py",), ("wrote session.zpf",)),
        (
            ("01_write_first_file.py", "02_read_it_back.py"),
            ("session 0: tcp", "GET / HTTP/1.1", "HTTP/1.1 200 OK"),
        ),
        (
            ("03_causal_order.py",),
            (
                "sorted by timestamp: [b'HTT', b'GET']",  # naive order is wrong
                "session.timeline(): [b'GET', b'HTT']",  # causal order is right
                "merged session sequenced: True",
                "verify(): OK",
            ),
        ),
        (
            ("01_write_first_file.py", "04_two_faces.py"),
            ('"type":"record"', "round trip: byte-identical"),
        ),
        (
            ("05_rest_raw_input.py",),
            ("participant 0 (10.0.0.1:51000)", "offset  46:"),
        ),
        (
            ("05_rest_raw_input.py", "06_write_decoder.py"),
            (
                "file_kind: decode-stage",
                "dec:http-request",
                "dec:http-response",
                "check_coverage: []",  # every input byte accounted for
            ),
        ),
        (
            ("07_coverage.py",),
            (
                "auto-fill on:  []",  # decode_stage marked the tail for us
                "auto-filled reason='skipped'",  # ... as skipped, not undecodable
                "auto-fill off: coverage-gap",  # and the hole is caught without it
            ),
        ),
    ],
    ids=[
        "write",
        "write-then-read",
        "causal-order",
        "write-then-convert",
        "decode-raw-input",
        "write-decoder",
        "decode-coverage",
    ],
)
def test_example_scripts_run_clean(scripts: Sequence[str], expected: Sequence[str], tmp_path: Path):
    output = ""
    for script in scripts:
        result = _run(script, tmp_path)
        assert result.returncode == 0, result.stderr
        output += result.stdout
    for phrase in expected:
        assert phrase in output

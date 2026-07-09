from __future__ import annotations

import re

import zpf


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", zpf.__version__)

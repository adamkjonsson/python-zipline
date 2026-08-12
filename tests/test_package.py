from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import zpf

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _project_table() -> dict:
    if not PYPROJECT.is_file():
        pytest.skip("not running from a source checkout")
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", zpf.__version__)


def test_version_is_declared_only_in_pyproject() -> None:
    """`zpf.__version__` must agree with the one declaration of it.

    The version is static in `pyproject.toml` and read back from the
    installed distribution metadata, so these can only disagree if someone
    reintroduces a second source of truth.
    """
    assert _project_table()["version"] == zpf.__version__


def test_specification_url_names_the_implemented_spec_version() -> None:
    """The packaged Specification link must track `zpf.SPEC_VERSION`.

    Every `0.x` minor is a separate format with no compatibility path, so a
    stale link is not a near miss — it is the wrong document, handed to a
    reader with nothing to signal that it is wrong. This went stale once
    already (issue #48: the URL still said `v0.12` while the library
    implemented `0.16`), and it went stale *despite* the 0.16 port having a
    phase for exactly that prose sweep — because the URL lives in
    `pyproject.toml` rather than in `docs/`. Hence a test rather than a
    checklist line.
    """
    url = _project_table()["urls"]["Specification"]
    major, minor = zpf.SPEC_VERSION
    tag = re.search(r"/blob/([^/]+)/", url)
    assert tag is not None, f"cannot find a tag in the Specification URL: {url}"
    assert tag.group(1) == f"v{major}.{minor}", (
        f"Specification URL points at {tag.group(1)}, but this library "
        f"implements v{major}.{minor} — see the spec-bump checklist in "
        f"docs/dev/contributing.md"
    )

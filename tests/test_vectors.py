"""Conformance-vector harness for the upstream 0.12 vectors.

The vectors in ``tests/vectors/`` are hand-built from the specification's
normative text (see ``tests/vectors/VENDORED.md``). They are the acceptance
criteria for the 0.9 → 0.12 migration: each phase of the port is done when the
vectors it targets pass.

**The ratchet.** This library implements 0.9, so almost every vector fails
today. Rather than leave the suite red for the duration of the migration, a
vector case is a hard requirement only once its name is added to
:data:`KNOWN_PASSING`; every other case is ``xfail(strict=False)``. A case that
starts passing shows up as ``XPASS`` — that is the progress signal — and is then
promoted into :data:`KNOWN_PASSING` so it can never silently regress.

So the migration invariant is: ``KNOWN_PASSING`` only ever grows, and the suite
is green at every step.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import zpf

VECTORS = Path(__file__).parent / "vectors"

#: Vector cases that MUST pass. Grow it as phases land; never remove a name,
#: because that is the regression guard.
#:
#: Phase 1: the whole ``reject`` tier, plus the ``isolate`` cases whose
#: violation the indexing pass already detects. The remaining two need work
#: this library has not done yet — ``isolate-coverage-gap`` the coverage
#: check (Phase 5), ``isolate-sequenced-no-basis`` the ``sequenced_basis``
#: option (Phase 3) and hint-less detection (Phase 4).
KNOWN_PASSING: frozenset[str] = frozenset(
    {
        "isolate-duplicate-id",
        "isolate-undeclared-session",
        "isolate-unknown-source-kind",
        "reject-bad-magic",
        "reject-length-misaligned",
        "reject-payload-len-overrun",
        "reject-unknown-major",
        "reject-unknown-minor",
    }
)

#: Vectors that cannot pass until **upstream** fixes them, and why. Catalogued
#: in ``VECTOR-DEFECTS.md``. Kept distinct from the not-yet-ported xfails so
#: that nobody bends this implementation to match a broken fixture: if one of
#: these starts passing, the vector was fixed — or we got it wrong.
DEFECTIVE: dict[str, str] = {
    "undecoded-skipped": "defect 1: decode stage with no produced_by/produced_at",
    "undecoded-reason-class": "defect 1: decode stage with no produced_by/produced_at",
    "isolate-coverage-gap": "defect 1: second violation masks the coverage gap",
}

#: What each ``reject`` vector must be refused *for*. Asserting only that some
#: exception escaped is not enough: while the version gate is wrong, a 0.12
#: vector is refused for its ``version_major`` long before the check it actually
#: exercises is reached, so three of these passed for the wrong reason at
#: Phase 0. Matched case-insensitively against the error message.
_REJECT_REASONS: dict[str, str] = {
    "reject-bad-magic": "magic",
    "reject-unknown-major": "version_major",
    "reject-unknown-minor": "version_minor",
    "reject-length-misaligned": "length",
    "reject-payload-len-overrun": "payload_len",
}

#: What each ``isolate`` vector must be diagnosed *for*. Same discipline as
#: ``_REJECT_REASONS``, and it earns its keep: ``isolate-coverage-gap`` is
#: isolated today for a missing ``produced_by`` rather than for the coverage
#: gap it exists to test, so without this it would pass with the coverage
#: check unimplemented.
_ISOLATE_REASONS: dict[str, str] = {
    "isolate-coverage-gap": "coverage",
    "isolate-duplicate-id": "twice",
    "isolate-undeclared-session": "undeclared session",
    "isolate-unknown-source-kind": "unknown kind",
    "isolate-sequenced-no-basis": "sequenced_basis",
}

#: Fields the projection may render as a JSON number *or* a decimal string
#: ("Value encoding" — 64-bit fields beyond JSON's exact-integer range).
_WIDE_FIELDS = frozenset(
    {
        "session_id",
        "ts",
        "timestamp",
        "tick_hz",
        "time_epoch",
        "produced_at",
        "ts_first",
        "off_start",
        "off_end",
    }
)


@dataclass(frozen=True)
class Case:
    """One `.zpf` file under test.

    Attributes:
        name: Case id, e.g. ``"raw-minimal"`` or ``"chain/decoded"``.
        tier: ``"accept"``, ``"isolate"``, or ``"reject"``.
        path: The binary file under test.
        expected_jsonl: Its expected projection, if the tier defines one.
        summary: The manifest's one-line description, for failure messages.

    """

    name: str
    tier: str
    path: Path
    expected_jsonl: Path | None
    summary: str


def _load_cases() -> list[Case]:
    """Expand the manifest into one case per `.zpf` file.

    A multi-file vector (``chain``) yields one case per member, so every
    file under test is independently reported.

    Returns:
        Every case, ordered by name.

    """
    manifest = json.loads((VECTORS / "manifest.json").read_text())
    cases: list[Case] = []
    for entry in manifest["vectors"]:
        vector = VECTORS / entry["name"]
        members = sorted(vector.glob("*.zpf"))
        for path in members:
            jsonl = path.with_suffix(".jsonl")
            name = entry["name"] if len(members) == 1 else f"{entry['name']}/{path.stem}"
            cases.append(
                Case(
                    name=name,
                    tier=entry["tier"],
                    path=path,
                    expected_jsonl=jsonl if entry.get("has_jsonl") and jsonl.exists() else None,
                    summary=entry["summary"],
                )
            )
    return sorted(cases, key=lambda case: case.name)


CASES = _load_cases()


def _params(tier: str) -> list[Any]:
    """Build pytest params for one tier, xfailing all but the known-passing.

    Args:
        tier: The manifest tier to select.

    Returns:
        Params carrying an ``xfail`` mark unless the case is known to pass.

    """
    params = []
    for case in CASES:
        if case.tier != tier:
            continue
        if case.name in KNOWN_PASSING:
            marks: tuple[Any, ...] = ()
        else:
            reason = DEFECTIVE.get(case.name, "not yet ported to 0.12")
            marks = (pytest.mark.xfail(reason=reason, strict=False),)
        params.append(pytest.param(case, id=case.name, marks=marks))
    return params


def _normalise(value: Any, *, wide: bool = False) -> Any:
    """Canonicalise a projected value for semantic comparison.

    The projection is semantically — not textually — lossless, so a 64-bit
    field may legally be a number or a decimal string. Key order is handled
    by comparing dicts rather than text.

    Args:
        value: A decoded JSON value.
        wide: Whether this value sits under a 64-bit field name.

    Returns:
        The value with wide integers coerced to ``int``.

    """
    if isinstance(value, dict):
        return {key: _normalise(item, wide=key in _WIDE_FIELDS) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item, wide=wide) for item in value]
    if wide and isinstance(value, str):
        return int(value)
    return value


def _projection(path: Path) -> list[dict[str, Any]]:
    """Project a binary vector through the JSONL face.

    Args:
        path: The `.zpf` file to convert.

    Returns:
        One normalised object per output line.

    """
    sink = io.StringIO()
    zpf.binary_to_jsonl(path, sink)
    return [_normalise(json.loads(line)) for line in sink.getvalue().splitlines() if line.strip()]


def _expected(path: Path) -> list[dict[str, Any]]:
    """Read an expected-projection file.

    Args:
        path: The `.jsonl` file beside the vector.

    Returns:
        One normalised object per line.

    """
    return [_normalise(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def test_manifest_covers_the_tree() -> None:
    """Every vector directory is in the manifest, and vice versa."""
    manifest = json.loads((VECTORS / "manifest.json").read_text())
    declared = {entry["name"] for entry in manifest["vectors"]}
    present = {path.name for path in VECTORS.iterdir() if path.is_dir()}
    assert declared == present


def test_every_case_has_a_file() -> None:
    """The manifest expands to at least one file per declared vector."""
    assert len(CASES) >= 26
    for case in CASES:
        assert case.path.exists(), case.name


@pytest.mark.parametrize("case", _params("accept"))
def test_accept(case: Case) -> None:
    """A conformant file reads cleanly and projects to its expected JSONL."""
    with zpf.open(case.path) as reader:
        for session in reader.sessions():
            list(session.records())
        assert reader.diagnostics == [], f"{case.name}: {case.summary}"
    if case.expected_jsonl is not None:
        actual = _projection(case.path)
        expected = _expected(case.expected_jsonl)
        assert len(actual) == len(expected), f"{case.name}: line count differs"
        for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
            assert got == want, f"{case.name}: line {index + 1} differs"


@pytest.mark.parametrize("case", _params("reject"))
def test_reject(case: Case) -> None:
    """Structural corruption is refused, and refused for the right reason.

    The reason matters as much as the refusal. A reader whose version gate
    rejects every 0.12 file passes this tier wholesale without ever
    exercising the framing checks the vectors are testing.
    """
    with pytest.raises(zpf.StructuralError) as caught, zpf.open(case.path) as reader:
        for session in reader.sessions():
            list(session.records())
    wanted = _REJECT_REASONS[case.name]
    assert wanted.lower() in str(caught.value).lower(), (
        f"{case.name}: rejected, but for the wrong reason — "
        f"expected {wanted!r} in {str(caught.value)!r}"
    )


@pytest.mark.parametrize("case", _params("isolate"))
def test_isolate(case: Case) -> None:
    """A semantic violation is isolated or rejected — never passed silently.

    The specification permits either outcome for this tier; what it forbids
    is accepting the file with nothing reported. (The upstream README is
    stricter than the spec here — see ``tests/vectors/VENDORED.md``.)
    """
    try:
        with zpf.open(case.path) as reader:
            for session in reader.sessions():
                list(session.records())
            diagnostics = list(reader.diagnostics)
    except zpf.StructuralError:
        pytest.fail(f"{case.name}: semantic violation rejected as structural corruption")
    except zpf.SemanticError as exc:
        reported = str(exc)  # Rejecting the file outright is permitted.
    else:
        assert diagnostics, f"{case.name}: accepted silently — {case.summary}"
        reported = " ".join(item.message for item in diagnostics)
    wanted = _ISOLATE_REASONS[case.name]
    assert wanted.lower() in reported.lower(), (
        f"{case.name}: isolated, but for the wrong reason — "
        f"expected {wanted!r} in {reported!r}"
    )

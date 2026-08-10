"""Conformance-vector harness for the upstream 0.16 vectors.

The vectors in ``tests/vectors/`` are hand-built from the specification's
normative text (see ``tests/vectors/VENDORED.md``). They are the acceptance
criteria for the 0.14 → 0.16 migration: each phase of the port is done when the
vectors it targets pass.

**The ratchet.** A vector case is a hard requirement only once its name is added
to :data:`KNOWN_PASSING`; every other case is ``xfail(strict=False)``. That keeps
the suite green across a migration that starts with every file unreadable — until
the version gate moves, a 0.16 file is refused whatever else is implemented. A
case that starts passing shows up as ``XPASS`` — that is the progress signal —
and is then promoted into :data:`KNOWN_PASSING` so it can never silently
regress.

So the migration invariant is: ``KNOWN_PASSING`` only ever grows, and the suite
is green at every step.

The ratchet was reset at Phase 0 of the 0.16 port, when the tree was re-vendored.
It is not a record of what this library once passed — under 0.14 it passed all 39
vectors then shipped — only of what it passes against *these* files.

**Three tests here are not ratchet-gated**, because they are parametrized over
every readable case rather than over a tier: the escape-contract test, the
JSONL → binary direction, and the canonical re-encode. Phase 0 held them behind
a version-gate mark, Phase 1 moved the gate and retired it, and Phase 2 emptied
what remained. All three now run unmarked against every vector except the
:data:`DEFECTIVE` ones — so a regression in either face shows up as a failure
rather than as a silently missing XPASS.
"""

from __future__ import annotations

import dataclasses
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
#: Phase 4: per-participant classification, replacing file purity.
#: ``mixed-derivation`` is the shape the old file-wide rule could not express
#: — one file decoding one session while passing another through. The three
#: ``isolate`` vectors are rules that had nowhere to live until the unit was
#: the stream: a participant resolving to two layers, a ``zpf``-sourced
#: participant bound to no input, and a decoder declaring a layer this
#: version does not define.
#:
#: Phase 5: the Undecoded body read by the referenced source's ``kind``.
#: ``undecoded-in-capture`` is the conformant case — a reassembler declaring
#: an overlap it discarded, with no ids and no derived header — and
#: ``isolate-hole-against-capture`` the barred one, where the gap is already
#: carried by the reassembled stream's own hole-inclusive offsets.
#:
#: Two vectors are still out and both are Phase 6's: ``isolate-unmarked-break``
#: needs the Discontinuity predicate, and ``isolate-self-derived`` a check
#: only a reader that knows the path it opened can make.
#:
#: ``splice`` is one name for two files, because its violation belongs to
#: neither of them — 52 names for 59 files, which is the honest arithmetic.
#: :data:`PAIRWISE` routes it to :func:`test_a_pairwise_vector_is_caught`, and
#: the per-file tiers skip it.
KNOWN_PASSING: frozenset[str] = frozenset(
    {
        "advisory-transport-content-type",
        "annotator-decoded",
        "broken-chain",
        "chain/annotated",
        "chain/decoded",
        "chain/raw",
        "custom-block",
        "decoded-basic",
        "descriptive-metadata",
        "discontinuity-known-width",
        "discontinuity-unknown-width",
        "escape-reserved-flag-bit",
        "escape-unknown-block",
        "escape-unknown-enum",
        "escape-unknown-option",
        "external-session-id",
        "file-clock-metadata",
        "filtered-decoded",
        "hintless-merge-backwards-ts",
        "isolate-coverage-gap",
        "isolate-discontinuity-in-raw",
        "isolate-duplicate-id",
        "isolate-extent-exceeds-coverage",
        "isolate-extents-disagree",
        "isolate-hole-against-capture",
        "isolate-mixed-layer-participant",
        "isolate-sequenced-no-basis",
        "isolate-unbound-zpf-stream",
        "isolate-undeclared-session",
        "isolate-unknown-output-layer",
        "isolate-unknown-source-kind",
        "merge-timestamp-tie",
        "mixed-derivation",
        "partially-hinted-sequenced",
        "passthrough-discontinuity",
        "passthrough-transport",
        "proxy-decoded",
        "raw-minimal",
        "reassembler-declared",
        "reject-bad-magic",
        "reject-length-misaligned",
        "reject-payload-len-overrun",
        "reject-unknown-major",
        "reject-unknown-minor",
        "reordered-decoded",
        "sequenced-basis",
        "session-fan-out",
        "sessionization-stage",
        "splice",
        "tunnel/http",
        "tunnel/packets",
        "undecoded-in-capture",
        "undecoded-reason-class",
        "undecoded-skipped",
    }
)

#: Vectors that cannot pass until **upstream** fixes them, and why. Catalogued
#: in ``VECTOR-DEFECTS.md``. Kept distinct from the not-yet-ported xfails so
#: that nobody bends this implementation to match a broken fixture: if one of
#: these starts passing, the vector was fixed — or we got it wrong.
#:
#: Two entries at ``v0.16``, both the same defect: ``tunnel/inner.jsonl`` and
#: ``tunnel/outer.jsonl`` spell the Session flow key ``"flow_key"``, but the
#: normative JSONL mapping lists it among the **brevity aliases** as ``"key"``
#: — and ``descriptive-metadata.jsonl`` in this same suite writes ``"key"``.
#: See ``VECTOR-DEFECTS.md`` defect 4. Our face follows the mapping table, so
#: these two cannot pass until upstream fixes them, and must not be chased at
#: Phase 2 when the other ``tunnel/`` members turn green.
DEFECTIVE: dict[str, str] = {
    "tunnel/inner": "defect 4: .jsonl writes flow_key where the mapping says key",
    "tunnel/outer": "defect 4: .jsonl writes flow_key where the mapping says key",
}

#: What each ``reject`` vector must be refused *for*. Asserting only that some
#: exception escaped is not enough: while the version gate is behind the
#: vectors, a file is refused for its stamped version long before the check it
#: actually exercises is reached. That is exactly the state at Phase 0 of each
#: port — three of these passed for the wrong reason at 0.12's, and two did at
#: 0.14's and again at 0.16's, held out of :data:`KNOWN_PASSING` until the gate
#: moves in Phase 1. Matched case-insensitively against the error message.
_REJECT_REASONS: dict[str, str] = {
    "reject-bad-magic": "magic",
    "reject-unknown-major": "version_major",
    "reject-unknown-minor": "version_minor",
    "reject-length-misaligned": "length",
    "reject-payload-len-overrun": "payload_len",
}

#: What each ``isolate`` vector must be diagnosed *for*. Same discipline as
#: ``_REJECT_REASONS``, and it earned its keep against the 0.12 vectors:
#: ``isolate-coverage-gap`` carried a second violation, so it was isolated for a
#: missing ``produced_by`` rather than for the coverage gap it exists to test and
#: would have passed with the coverage check unimplemented. Upstream now enforces
#: one violation per negative vector, but that says the fixtures are right — not
#: that we detected the right thing.
#:
#: The ``splice/`` pair is absent because its violation belongs to neither file
#: on its own; a case with no entry here xfails before the lookup.
#:
#: The six ``isolate`` vectors new in 0.15/0.16 are likewise absent until the
#: phase that implements each — ``isolate-hole-against-capture`` and
#: ``isolate-unknown-output-layer`` at Phase 5, the other four at Phases 4 and
#: 6. Their diagnostic wording is not decided yet, and inventing it here would
#: assert against a message no code produces.
_ISOLATE_REASONS: dict[str, str] = {
    "isolate-coverage-gap": "neither decoded nor marked",
    "isolate-discontinuity-in-raw": "discontinuity",
    "isolate-duplicate-id": "twice",
    "isolate-extent-exceeds-coverage": "declared extent",
    "isolate-extents-disagree": "disagree",
    "isolate-hole-against-capture": "only the bytes-exist class is available",
    "isolate-mixed-layer-participant": "resolves to two layers",
    "isolate-sequenced-no-basis": "sequenced_basis",
    "isolate-unbound-zpf-stream": "neither origin nor records with spans",
    "isolate-unknown-output-layer": "does not define",
    "isolate-undeclared-session": "undeclared session",
    "isolate-unknown-source-kind": "unknown kind",
}

#: Syntax new in 0.13/0.14, by the option id carrying it. Each must be parsed
#: into a typed field rather than preserved through the unknown-option escape.
#:
#: **Nothing joins this at 0.16.** The port's one piece of new syntax,
#: ``output_layer``, is a Decoder *body* field rather than an option — there is
#: no id for it and no escape it could hide in, which is why the specification
#: chose a body field. A dropped ``output_layer`` shows up in the re-encode and
#: projection tests instead.
_NEW_OPTION_IDS: dict[int, str] = {
    0x0015: "transform_params_digest",
    0x0054: "external_session_id",
    0x00C1: "input_extents",
    0x00D0: "width",
    0x00D1: "reason (Discontinuity)",
}

#: The one block type that MUST stay unknown. ``escape-unknown-block`` ships it
#: to prove a reader skips what it does not recognize rather than failing, so a
#: reader that "recognized" it would have failed the extension contract.
_DELIBERATELY_UNKNOWN: frozenset[int] = frozenset({0x42})

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
        "extent",
        "width",
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


def _pairwise_vectors() -> list[str]:
    """Name the vectors whose violation belongs to a *pair* of files.

    A multi-file entry in the ``accept`` tier (``chain``) is still judged
    file by file: each member is conformant on its own. A multi-file entry
    in a negative tier is the other thing — ``splice`` ships two files that
    are each individually clean, so no per-file test can see what is wrong.

    Returns:
        The manifest names of those vectors, in manifest order.

    """
    manifest = json.loads((VECTORS / "manifest.json").read_text())
    return [
        entry["name"]
        for entry in manifest["vectors"]
        if entry.get("files") and entry["tier"] != "accept"
    ]


PAIRWISE = _pairwise_vectors()


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


def _readable_params() -> list[Any]:
    """Build unmarked params for every vector a conformant reader can parse.

    The ``reject`` tier is excluded: those files are structurally corrupt by
    construction, so there are no blocks to inspect.

    Returns:
        One param per readable case, in name order.

    """
    return [pytest.param(case, id=case.name) for case in CASES if case.tier != "reject"]


def _projected_params() -> list[Any]:
    """Build params for every vector shipping an expected projection.

    Phase 2 emptied the port's own gate here: the JSONL face reads and
    writes ``output_layer``, so every projected vector rebuilds its binary.
    What is left is :data:`DEFECTIVE`, which is upstream's to fix and not a
    phase to wait for.

    Returns:
        One param per case with a ``.jsonl`` beside it, in name order.

    """
    params = []
    for case in CASES:
        if case.expected_jsonl is None:
            continue
        marks: tuple[Any, ...] = ()
        if case.name in DEFECTIVE:
            marks = (pytest.mark.xfail(reason=DEFECTIVE[case.name], strict=False),)
        params.append(pytest.param(case, id=case.name, marks=marks))
    return params


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
        if case.name.split("/")[0] in PAIRWISE:
            continue  # judged as a pair; see test_a_pairwise_vector_is_caught
        if case.name in KNOWN_PASSING:
            marks: tuple[Any, ...] = ()
        else:
            reason = DEFECTIVE.get(case.name, "not yet ported to 0.16")
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
    """The manifest expands to exactly the files `v0.16` ships.

    The count is exact rather than a floor, so a half-copied re-vendoring
    fails here instead of quietly shrinking the suite: 53 manifest entries,
    of which ``chain`` expands to three files, ``splice`` to two and
    ``tunnel`` — the largest fixture the suite has — to four.
    """
    assert len(CASES) == 59
    for case in CASES:
        assert case.path.exists(), case.name


@pytest.mark.parametrize("case", _readable_params())
def test_new_syntax_is_recognized_rather_than_escaped(case: Case) -> None:
    """No 0.13/0.14 id reaches ``extra_options``, and no block stays unknown.

    This is the acceptance test the vector tiers cannot give. Every ``accept``
    vector carrying new syntax also ships a ``.jsonl``, which
    :func:`test_accept` compares, so none of them turns green until the
    projection lands in Phase 2 — while the escape contract means every one of
    them *reads* cleanly all along, with ``input_extents`` sitting in
    ``extra_options`` and the Discontinuity block preserved as an
    ``UnknownBlock``. Without this test the whole of Phase 1 could be skipped
    and nothing would go red.
    """
    with zpf.open(case.path) as reader:
        blocks = list(reader.blocks())
    for index, block in enumerate(blocks):
        escaped = [
            _NEW_OPTION_IDS[opt.option_id]
            for opt in getattr(block, "extra_options", ())
            if opt.option_id in _NEW_OPTION_IDS
        ]
        assert not escaped, (
            f"{case.name}: block {index} ({type(block).__name__}) preserved "
            f"{escaped} as raw options instead of parsing them"
        )
        if isinstance(block, zpf.UnknownBlock):
            assert block.block_type in _DELIBERATELY_UNKNOWN, (
                f"{case.name}: block {index} of type 0x{block.block_type:02X} is not recognized"
            )


@pytest.mark.parametrize("case", _projected_params())
def test_the_shipped_jsonl_converts_back_to_the_binary(case: Case) -> None:
    """The projection is lossless in the direction the tier tests do not cover.

    :func:`test_accept` compares binary → JSONL. This is the other way: the
    vector's own ``.jsonl`` is the input, and what comes out must be the blocks
    the ``.zpf`` holds. Semantic rather than byte comparison, for the reason
    the re-encode test gives — a converter may legally re-chunk ``spans`` and
    order options as it likes.

    It also reaches the two ``splice/`` files, whose ``.jsonl`` no tier test
    looks at: they are ``isolate``, so :func:`test_accept` never sees them.
    """
    assert case.expected_jsonl is not None
    with zpf.open(case.path) as reader:
        expected = list(reader.blocks())
    sink = io.BytesIO()
    zpf.jsonl_to_binary(io.StringIO(case.expected_jsonl.read_text()), sink)
    with zpf.open(io.BytesIO(sink.getvalue())) as converted:
        got = list(converted.blocks())
    assert got == expected, f"{case.name}: the JSONL does not rebuild the binary"


@pytest.mark.parametrize("case", _readable_params())
def test_a_vector_survives_a_canonical_re_encode(case: Case) -> None:
    """Re-encoding a parsed vector from scratch loses nothing.

    Not a *byte* comparison: :func:`dataclasses.replace` drops the cached
    original content, and the canonical encoding orders options by ascending
    registry id, which 18 of these files do not. Option order is not
    significant, so demanding the bytes back would fail on conformant input.
    What must survive is every field and every preserved occurrence — which is
    what catches a mis-declared struct or a dropped option in new syntax.
    """
    with zpf.open(case.path) as reader:
        blocks = list(reader.blocks())
    sink = io.BytesIO()
    writer = zpf.BlockWriter(sink)
    for block in blocks:
        writer.write(dataclasses.replace(block))
    with zpf.open(io.BytesIO(sink.getvalue())) as reparsed:
        again = list(reparsed.blocks())
    assert again == blocks, f"{case.name}: re-encoding changed the blocks"


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
    rejects every 0.14 file passes this tier wholesale without ever
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


def _pairwise_params() -> list[Any]:
    """Build params for the pairwise vectors, xfailing all but known-passing."""
    params = []
    for name in PAIRWISE:
        marks: tuple[Any, ...] = ()
        if name not in KNOWN_PASSING:
            marks = (pytest.mark.xfail(reason="not yet ported to 0.16", strict=False),)
        params.append(pytest.param(name, id=name, marks=marks))
    return params


def _stage_order(vector: str) -> tuple[Path, Path]:
    """Work out which member decodes which, from the Source each declares.

    Returns:
        ``(stage2, stage1)`` — the file citing a sibling, and the sibling it
        cites.

    """
    members = {path.name: path for path in sorted((VECTORS / vector).glob("*.zpf"))}
    for path in members.values():
        with zpf.open(path) as reader:
            cited = [
                source.uri
                for source in reader.sources.values()
                if source.kind == zpf.SourceKind.ZPF_INPUT and source.uri in members
            ]
        if cited:
            return path, members[cited[0]]
    msg = f"{vector}: no member cites a sibling, so there is no stage order to find"
    raise AssertionError(msg)


@pytest.mark.parametrize("vector", _pairwise_params())
def test_a_pairwise_vector_is_caught(vector: str) -> None:
    """A violation that belongs to the pair, not to either file.

    ``splice`` is built so this cannot be faked: stage 1 declares a break
    and breaks no rule, stage 2 is well-framed with complete coverage and
    nothing wrong on its face. **A harness that tests files individually
    passes it** — so this asserts both halves: each file is clean alone, and
    the pair is not.
    """
    stage2, stage1 = _stage_order(vector)
    for path in (stage1, stage2):
        with zpf.open(path) as reader:
            for session in reader.sessions():
                list(session.records())
            assert reader.diagnostics == [], f"{path.name} should be clean on its own"
        assert zpf.check_extents(path) == [], f"{path.name} should be clean on its own"
    findings = zpf.check_splice(stage2, stage1)
    assert findings, f"{vector}: the pair passed, and it must not"
    assert all(f.category == "discontinuity-splice" for f in findings)


def test_a_clean_pair_reports_nothing() -> None:
    """The chain's own hops splice nothing, so the checker stays quiet."""
    chain = VECTORS / "chain"
    assert zpf.check_splice(chain / "annotated.zpf", chain / "decoded.zpf") == []
    assert zpf.check_splice(chain / "decoded.zpf", chain / "raw.zpf") == []

# Vendored conformance vectors

These files are copied **verbatim** from the Zipline Payload Format
specification repository. They are not ours to edit.

| | |
|---|---|
| Source | <https://github.com/adamkjonsson/zipline> |
| Path | `vectors/` |
| Tag | `v0.14` |
| Commit | `89c54e21b03d1bbc6c33b2959651cb5134796f2a` |
| Vendored | 2026-08-06 |

## What was and was not copied

Copied: `manifest.json`, `README.md`, and all 39 vector directories.

**Not** copied: `build.py` and `check.py`. Those are the upstream *generator* and
self-consistency checker — tooling for maintaining the vectors, not fixtures for
consuming them. Our harness is [`../test_vectors.py`](../test_vectors.py).

Since `0.14` `check.py` also enforces **capability coverage**: it parses the
option-id registry and the block-type table out of the specification and requires
every entry to appear in some vector, so new syntax cannot ship uncovered. That
makes it more useful upstream, not more useful here — it rules on the *suite's*
completeness, and deliberately parses no block body, so it is not a second
conformance harness for us to run.

## Why they are checked in rather than fetched

Fetching by tag at test time would keep them authoritative, but makes the suite
network-dependent and CI flaky, and hides vector changes from code review.
Checked in, a spec bump shows up as a reviewable diff.

## Refreshing them

Re-copy from the new tag and update the table above. Do not hand-edit a vector:
per the upstream ground rules they are subordinate to the normative text, so a
vector that seems wrong is a question for the spec repository, not a local patch.

## Known defects

**None at `v0.14`.** Two were found against `v0.12` and both were fixed
upstream; [`VECTOR-DEFECTS.md`](../../VECTOR-DEFECTS.md) at the repository root
is now the closed record of them.

Both left a mark on the harness, and it stays:

- **The `isolate` tier.** The `0.12` README's tier table permitted a reader to
  reject *or* isolate, while its prose called rejecting "as wrong as" accepting
  silently. The specification permits either, so
  [`../test_vectors.py`](../test_vectors.py) followed the spec and asserted only
  that the violation is not passed silently. The README now says the same —
  "Rejecting an `isolate` vector, with a diagnostic, **is conformant**" — so the
  harness and the fixtures agree rather than the harness overriding them.
- **Negative vectors carrying two violations.** `isolate-coverage-gap` could be
  passed without implementing the coverage check, because it was also missing a
  derived file's mandatory `produced_by`/`produced_at`. Fixed in `0.13`, and
  since then every entry declares a `violations` count that upstream's `check.py`
  requires to agree with its tier. The harness still asserts what each negative
  vector is diagnosed *for* rather than merely that something was reported: the
  count being right upstream does not tell us we detected the right one.

A vector that seems wrong remains a question for the spec repository, not a local
patch — see *Refreshing them* above.

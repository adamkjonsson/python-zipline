# Vendored conformance vectors

These files are copied **verbatim** from the Zipline Payload Format
specification repository. They are not ours to edit.

| | |
|---|---|
| Source | <https://github.com/adamkjonsson/zipline> |
| Path | `vectors/` |
| Tag | `v0.12` |
| Commit | `c291afcf0c7e13078b78723763ae8e4db937a8d7` |
| Vendored | 2026-07-31 |

## What was and was not copied

Copied: `manifest.json`, `README.md`, and all 26 vector directories.

**Not** copied: `build.py` and `check.py`. Those are the upstream *generator* and
self-consistency checker — tooling for maintaining the vectors, not fixtures for
consuming them. Our harness is [`../test_vectors.py`](../test_vectors.py).

## Why they are checked in rather than fetched

Fetching by tag at test time would keep them authoritative, but makes the suite
network-dependent and CI flaky, and hides vector changes from code review.
Checked in, a spec bump shows up as a reviewable diff.

## Refreshing them

Re-copy from the new tag and update the table above. Do not hand-edit a vector:
per the upstream ground rules they are subordinate to the normative text, so a
vector that seems wrong is a question for the spec repository, not a local patch.

## Known defects

Some vectors and one part of the upstream `README.md` are wrong. They are
catalogued in [`VECTOR-DEFECTS.md`](../../VECTOR-DEFECTS.md) at the repository
root, to be reported upstream in one batch — do not fix them here, since these
files are vendored verbatim.

The two that shape the harness:

- **The `isolate` tier.** The README's tier table permits a reader to reject
  *or* isolate; its prose then calls rejecting "as wrong as" accepting silently.
  The specification permits either, so [`../test_vectors.py`](../test_vectors.py)
  follows the spec and asserts only that the violation is not passed silently.
- **Negative vectors carrying two violations.** `isolate-coverage-gap` can be
  passed without implementing the coverage check, because it is also missing a
  derived file's mandatory `produced_by`/`produced_at`. This is why the harness
  asserts what each negative vector is diagnosed *for*, not merely that
  something was reported.

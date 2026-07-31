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

## Note on the `isolate` tier

The upstream `README.md` is internally inconsistent about this tier, and our
harness deliberately follows the **specification** rather than the README:

- The README's tier table says a reader "MAY reject the file *or* discard the
  smallest unit it can soundly isolate" — matching the spec's semantic-violation
  tier.
- The README's prose then says "a reader that *rejects* an `isolate` vector is as
  wrong as one that accepts it silently", which contradicts the MAY.

The spec is normative and permits either, so [`../test_vectors.py`](../test_vectors.py)
accepts both outcomes and asserts only what the spec actually requires: the
violation must not pass **silently**. Reported upstream.

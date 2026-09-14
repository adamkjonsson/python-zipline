# Vendored conformance vectors

These files are copied **verbatim** from the Zipline Payload Format
specification repository. They are not ours to edit.

| | |
|---|---|
| Source | <https://github.com/adamkjonsson/zipline> |
| Path | `vectors/` |
| Tag | `v0.20` |
| Commit | `55d49936fd6c90142cdca380c8a5205d2a3d82e6` |
| Vendored | 2026-09-14 |

## What was and was not copied

Copied: `manifest.json`, `README.md`, and all 55 vector directories — verified
byte-identical to the tag with `diff -r`. 55 entries expand to 63 files, because
`chain` and `merge` ship three each, `splice` two and `tunnel` four.

**Not** copied: `build.py` and `check.py`. Those are the upstream *generator* and
self-consistency checker — tooling for maintaining the vectors, not fixtures for
consuming them. Our harness is [`../test_vectors.py`](../test_vectors.py).

Since `0.14` `check.py` also enforces **capability coverage**: it parses the
option-id registry and the block-type table out of the specification and requires
every entry to appear in some vector, so new syntax cannot ship uncovered. Since
`0.16` it also carries `RETIRED_CLAIMS`, a ratchet holding claims the model has
retired from reappearing in the specification text. Both rule on the *suite's*
and the *document's* consistency rather than on a reader's conformance, and
neither parses a block body, so `check.py` is still not a second harness for us
to run.

## One thing changed shape in `manifest.json`

`0.16` adds an optional **`advisory: true`** on an `accept` entry, which then
declares **1** violation rather than 0. It is a key rather than a fourth tier
because a tier names what a *reader does*, and a reader accepts these files
completely. Our harness needs a path for it that neither `accept` nor `isolate`
provides. There are two at `0.20`: `advisory-transport-content-type` and
`advisory-transport-role`, one per label the transport-layer bar names.

`0.20` adds **`extents`** on every single-file `accept` entry: a list of
`{session_id, pid, extent}` per participant stream, in the `input_extents`
entry shape, stating what a reader must *compute*. It exists because the tier
asserts a projection and a violation count, and neither catches a reader that
computes the wrong offset in silence — which is what this library did to
`unplaceable-below-origin` at the `0.19` re-vendor, and reported as
[zipline#140](https://github.com/adamkjonsson/zipline/issues/140). The harness
reads the key directly (`test_an_accept_vector_puts_its_bytes_where_it_says`),
so the hand-transcribed table it replaced is gone. 31 entries, 37 streams.

The same issue's other half — a SHOULD-report on a `violations: 0` vector — got
a README sentence rather than a key: a manifest key asserting the report would
promote the SHOULD to a MUST through the suite, which the vectors' ground rule
2 forbids. So `unplaceable-below-origin` is asserted as a clean accept, and its
`expect` now says so.

## Why they are checked in rather than fetched

Fetching by tag at test time would keep them authoritative, but makes the suite
network-dependent and CI flaky, and hides vector changes from code review.
Checked in, a spec bump shows up as a reviewable diff.

## Refreshing them

Re-copy from the new tag and update the table above. Do not hand-edit a vector:
per the upstream ground rules they are subordinate to the normative text, so a
vector that seems wrong is a question for the spec repository, not a local patch.

## Known defects

**None open at `v0.20`.** Defects 5 and 6 — three vectors whose `.zpf`
disagreed with their own `.jsonl`, found at the `0.19` re-vendor by projecting
every file and diffing — were fixed upstream in `0.20`
([zipline#141](https://github.com/adamkjonsson/zipline/issues/141)), which also
made `build.py` compare a vector's two faces at registration, so the class
cannot recur. That guard is the part that outlives the fix. So `DEFECTIVE` in
[`../test_vectors.py`](../test_vectors.py) is empty, and there is no fixture the
implementation must be kept away from.

Six have been found in all — two against `v0.12`, one against `v0.15`, one
against `v0.16` and two against `v0.19` — and every one was fixed upstream;
[`VECTOR-DEFECTS.md`](../../VECTOR-DEFECTS.md) is the closed record of them.
The projection sweep was repeated at this re-vendor: 43 files carry both faces,
and none disagree.

The third is worth knowing about while reading these files, because it is the one
whose fix changed a vector's **bytes**: `undecoded-in-capture` shipped at `v0.15`
with `session_id = 7`, and `0.16` settled the rule it was caught between —
against a `capture` Source the ids are unused and MUST be written `0`. We never
vendored `v0.15`, so the file arrives here already correct; it is catalogued
because it is the only defect so far that was *not* vector-side alone, the text
having disagreed with itself.

The first two left a mark on the harness, and it stays:

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

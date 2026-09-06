# Vendored conformance vectors

These files are copied **verbatim** from the Zipline Payload Format
specification repository. They are not ours to edit.

| | |
|---|---|
| Source | <https://github.com/adamkjonsson/zipline> |
| Path | `vectors/` |
| Tag | `v0.19` |
| Commit | `f0b1464b0c26d28a91f7d0e537bf3647bf6b302a` |
| Vendored | 2026-09-06 |

## What was and was not copied

Copied: `manifest.json`, `README.md`, and all 53 vector directories — verified
byte-identical to the tag with `diff -r`. 53 entries expand to 59 files, because
`chain` ships three, `splice` two and `tunnel` four.

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
provides. There are two at `0.19`: `advisory-transport-content-type` and
`advisory-transport-role`, one per label the transport-layer bar names.

**And one shape the manifest still cannot express**, which our harness works
around rather than inherits. `unplaceable-below-origin` declares `violations: 0`
and is not `advisory` — correct, since `0.19` withdrew the origin floor and the
file breaks no rule — while §Referencing says a reader SHOULD report the record
and the entry's own `expect` opens "ACCEPT, and REPORT". No field carries *breaks
no rule and is still reported*. Filed upstream as
[zipline#140](https://github.com/adamkjonsson/zipline/issues/140), together with
the reason `_ACCEPT_EXTENTS` exists in
[`../test_vectors.py`](../test_vectors.py): the tier asserts a projection and a
violation count, and neither catches a reader that computes the wrong offset in
silence.

## Why they are checked in rather than fetched

Fetching by tag at test time would keep them authoritative, but makes the suite
network-dependent and CI flaky, and hides vector changes from code review.
Checked in, a spec bump shows up as a reviewable diff.

## Refreshing them

Re-copy from the new tag and update the table above. Do not hand-edit a vector:
per the upstream ground rules they are subordinate to the normative text, so a
vector that seems wrong is a question for the spec repository, not a local patch.

## Known defects

**None open at `v0.19`**, for the first time since the register was started.
Defect 4 — `tunnel/inner.jsonl` and `tunnel/outer.jsonl` spelling the Session
flow key `"flow_key"` where the normative JSONL mapping lists it among the
brevity aliases as `"key"` — was fixed upstream in `0.17`
([zipline#104](https://github.com/adamkjonsson/zipline/issues/104)), which also
added a `check.py` guard building the projection's key vocabulary from the
specification's own tables. That guard is the part that outlives the fix. So
`DEFECTIVE` in [`../test_vectors.py`](../test_vectors.py) is empty, and there is
no fixture the implementation must be kept away from.

Four have been found in all — two against `v0.12`, one against `v0.15` and one
against `v0.16` — and every one was fixed upstream;
[`VECTOR-DEFECTS.md`](../../VECTOR-DEFECTS.md) is the closed record of them.

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

# Vendored conformance vectors

These files are copied **verbatim** from the Zipline Payload Format
specification repository. They are not ours to edit.

| | |
|---|---|
| Source | <https://github.com/adamkjonsson/zipline> |
| Path | `vectors/` |
| Tag | `v0.16` |
| Commit | `50fae23dba703ffcdc7b3aabc57988996762ade8` |
| Vendored | 2026-08-09 |

## What was and was not copied

Copied: `manifest.json`, `README.md`, and all 53 vector directories — verified
byte-identical to the tag with `diff -r`.

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
completely. `advisory-transport-content-type` is the only one so far, and it is
the format's first violation that accepts — our harness needs a path for it that
neither `accept` nor `isolate` provides.

## Why they are checked in rather than fetched

Fetching by tag at test time would keep them authoritative, but makes the suite
network-dependent and CI flaky, and hides vector changes from code review.
Checked in, a spec bump shows up as a reviewable diff.

## Refreshing them

Re-copy from the new tag and update the table above. Do not hand-edit a vector:
per the upstream ground rules they are subordinate to the normative text, so a
vector that seems wrong is a question for the spec repository, not a local patch.

## Known defects

**One open at `v0.16`** — `tunnel/inner.jsonl` and `tunnel/outer.jsonl` spell
the Session flow key `"flow_key"` where the normative JSONL mapping lists it
among the brevity aliases as `"key"`, and where `descriptive-metadata.jsonl` in
this same tree writes `"key"`. Both files are held in `DEFECTIVE` in
[`../test_vectors.py`](../test_vectors.py) rather than accommodated. Details as
defect 4 in [`VECTOR-DEFECTS.md`](../../VECTOR-DEFECTS.md).

Three others have been found — two against `v0.12` and one against `v0.15` — and
all three were fixed upstream; the same file is the closed record of them.

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

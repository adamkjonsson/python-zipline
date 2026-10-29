# Vendored conformance vectors

These files are copied **verbatim** from the Zipline Payload Format
specification repository. They are not ours to edit.

| | |
|---|---|
| Source | <https://github.com/adamkjonsson/zipline> |
| Path | `vectors/` |
| Tag | `v0.24` |
| Commit | `f15bb815c5fe67563b8792c2c78bd4172be2c10d` |
| Vendored | 2026-10-29 |

## What was and was not copied

Copied: `manifest.json`, `README.md`, and all 77 vector directories — verified
byte-identical to the tag with `diff -r`, extracted with `git archive` from the
tag rather than copied from a working tree. 77 entries expand to 90 files:
`chain` and `merge` ship three each, `tunnel` four, and six pairs two each
(`splice`, `decoded-past-fin`, `isolate-transport-splice` and three
`sessionized-*`).

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
provides. There are three at `0.22`, unchanged since `0.21`: `advisory-transport-content-type` and
`advisory-transport-role`, one per label the transport-layer bar names, and
`advisory-transport-adjacency`, for the body field that bar was extended to.

`0.20` adds **`extents`** on every single-file `accept` entry: a list of
`{session_id, pid, extent}` per participant stream, in the `input_extents`
entry shape, stating what a reader must *compute*. It exists because the tier
asserts a projection and a violation count, and neither catches a reader that
computes the wrong offset in silence — which is what this library did to
`unplaceable-below-origin` at the `0.19` re-vendor, and reported as
[zipline#140](https://github.com/adamkjonsson/zipline/issues/140). The harness
reads the key directly (`test_an_accept_vector_puts_its_bytes_where_it_says`),
so the hand-transcribed table it replaced is gone. 37 entries, 44 streams at
`0.21`; 39 entries, 51 streams at `0.22`.

`0.21` changes no manifest key, but **every participant `.jsonl` line gains
`"adjacency"`** — a Participant Descriptor body field, and body fields always
project — so 55 projections changed while no `.zpf` byte did beyond the version
stamp. The seven vectors added are `stream-past-2gib`, `stream-wraps-seq` and
`session-split-capture-gap` for the unwrapping rule and the one hole it cannot
place; `unit-sequence-reversed`, `unit-sequence-nested`,
`isolate-unknown-adjacency` and `advisory-transport-adjacency` for the field.

The same issue's other half — a SHOULD-report on a `violations: 0` vector — got
a README sentence rather than a key: a manifest key asserting the report would
promote the SHOULD to a MUST through the suite, which the vectors' ground rule
2 forbids. So `unplaceable-below-origin` is asserted as a clean accept, and its
`expect` now says so.

`0.22` changes no manifest key and adds no syntax. Its four new vectors are
`sequenced-past-2gib-pure-ack` and `sequenced-ack-below-origin` for acks
unwrapping into the peer's offset space (zipline#151, #157),
`sessionized-across-units` for a transport output crossing its input's seams
(zipline#152), and `decap-ethernet` for the `net:` scheme (zipline#153). `tunnel`
was regenerated: its packets became real IPv4 + TCP, which moved
`packets.zpf`'s offsets and the spans and extents citing them (zipline#154, found
by this library against the release candidate). Every other file changed only
its version stamp.

`0.23` adds one manifest key and two vectors. **`merged`** gives, for every
unsequenced session in which two or more participants send records, the order
a reader must merge it into, as 1-based stored positions
([zipline#162](https://github.com/adamkjonsson/zipline/issues/162), asked for
by this library because the tiers could see a merge that stalled and nothing
else). Six vectors carry it, and the harness compares `timeline()` against it
(`test_an_accept_vector_merges_in_the_order_it_states`). The vectors are
`sequenced-pure-ack-and-fin` and its unsequenced twin `merge-pure-ack-and-fin`
(zipline#160, #161), for the rule that a record's end is one past the last
sequence number it occupies. Every other file changed only its version stamp
and the input digests that follow from it.

`0.24` changes one manifest key and adds nine vectors. **`merged` reaches
multi-file entries**
([zipline#163](https://github.com/adamkjonsson/zipline/issues/163)). The files
of one entry share session ids, so each item names its member by the stem
`files` lists: `{"file", "session_id", "order"}`. The key is required on a
multi-file entry and refused on a single-file one. The harness reads it per
member (`_merged_params`). Ten vectors carry `merged` now, `chain` among them.
The nine new vectors:

- `closed-final-ack` and the pair `decoded-past-fin/`, for a transport extent
  counted in sequence numbers and a FIN's position that is owed nothing
  (zipline#168);
- the pairs `sessionized-fragments/` and `sessionized-fragment-lost/`, and
  `capture-fragment-lost`, for a `message` record crossing its input's seams
  and for an incomplete reassembly (zipline#166, #170);
- the pairs `sessionized-across-break/` and `isolate-transport-splice/`, for a
  transport record crossing a *declared* break, with and without evidence
  (zipline#167);
- `udp-capture-copy` and `isolate-copy-differs`, for the new `copy` flag,
  `0x0100` (zipline#169).

`isolate-transport-splice` is the suite's second pairwise vector after
`splice`, and the harness routes it the same way. The multi-file *accept*
pairs gained a check of their own, `test_an_accept_pair_is_clean_as_a_pair`,
over the hops upstream's `check.py` vouches for. Every other file changed only
its version stamp and the input digests that follow from it.

`0.24` was the first release this library reviewed as a candidate
([`plans/SPEC-0.24-REVIEW.md`](../../plans/SPEC-0.24-REVIEW.md)). Its first
finding added a `MUST` and the vector `capture-fragment-lost`, so the count is
77 where the candidate had 76.

## Why they are checked in rather than fetched

Fetching by tag at test time would keep them authoritative, but makes the suite
network-dependent and CI flaky, and hides vector changes from code review.
Checked in, a spec bump shows up as a reviewable diff.

## Refreshing them

Re-copy from the new tag and update the table above. Do not hand-edit a vector:
per the upstream ground rules they are subordinate to the normative text, so a
vector that seems wrong is a question for the spec repository, not a local patch.

## Known defects

**None open at `v0.24`.** Defects 5 and 6 — three vectors whose `.zpf`
disagreed with their own `.jsonl`, found at the `0.19` re-vendor by projecting
every file and diffing — were fixed upstream in `0.20`
([zipline#141](https://github.com/adamkjonsson/zipline/issues/141)), which also
made `build.py` compare a vector's two faces at registration, so the class
cannot recur. That guard is the part that outlives the fix. So `DEFECTIVE` in
[`../test_vectors.py`](../test_vectors.py) is empty, and there is no fixture the
implementation must be kept away from.

Seven have been found in all — two against `v0.12`, one against `v0.15`, one
against `v0.16`, two against `v0.19` and one against the `0.22` release
candidate — and every one was fixed upstream;
[`VECTOR-DEFECTS.md`](../../VECTOR-DEFECTS.md) is the closed record of them.
The projection sweep was repeated at the `0.24` re-vendor: 69 files carry both
faces, and none disagree on any key this library projects. (At `0.23`, 55.) (At `0.21`, 49 files; the
new `adjacency` key was then also checked the other way, its byte in all 61
Participant blocks against the `.jsonl` line, 0 mismatches.)

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

# Migration plan: `python-zipline` 0.14 → 0.16

Against the [0.16 specification](https://github.com/adamkjonsson/zipline/blob/v0.16/docs/zipline-payload-format.md),
its [CHANGELOG](https://github.com/adamkjonsson/zipline/blob/v0.16/CHANGELOG.md)
and the 53 vectors at tag `v0.16`. We ship `0.14`; there is no `0.15` release of
this library, so this is a **two-release jump** and the plan covers both deltas.

---

## What we are actually migrating to

**`0.15` is the feature release and carries all the risk.** Three things:

1. **Provenance and layer become independent axes**, and `raw` is retired as a
   normative term. A stream's offset space no longer follows from its Source
   `kind`. All four cells of the table are now legal, and one file may hold
   streams in different cells.
2. **`output_layer`** — a `u8` in the Decoder Descriptor **body** (`0 = decoded`,
   `1 = transport`), landing on the two `_reserved` bytes. The layer rule becomes
   *layer = decoder present ? that decoder's `output_layer` : transport*. This is
   the release's only new syntax, and it makes **reassembly a decoder**.
3. **The origination duty** — a stage MUST emit a Discontinuity between two
   adjacent units of its **own** output wherever they do not join, not only when
   its input carried one. This is what makes `0.14`-conformant files
   non-conformant.

**`0.16` is corrective, and it is our review's release.** Twelve items, ten of
which are [`SPEC-0.15-REVIEW.md`](SPEC-0.15-REVIEW.md) findings adopted verbatim.
No new block, no new option, no body-layout change. What matters for us:

- **Both blockers are resolved.** Finding 2 (`undecoded-in-capture` unparseable)
  and Finding 4 (the Discontinuity check stated without a predicate) were the two
  that stopped a from-the-text implementation. `0.16` settles both, so this port
  can be written from the text with no open ruling.
- **Finding 3 is adopted**, which licenses caching the layer per participant
  rather than resolving it per record — a real simplification, not just a rule.
- **Finding 9 is adopted as *advisory***, which introduces the format's first
  violation that **accepts**. That reaches our vector harness, not just the
  checker.

**Vectors: 39 → 53**, no removals. Fourteen new, one changed
(`reordered-decoded` gains a Discontinuity at its seam), one byte-changed
(`undecoded-in-capture`, `session_id` `7` → `0`).

---

## What changed against the review's sizing

[`SPEC-0.15-REVIEW.md`](SPEC-0.15-REVIEW.md) § *Implementation impact* is still
the right sizing document. `0.16` moves five things in it:

| Review finding | Outcome in `0.16` | Effect on the port |
|---|---|---|
| **F2** — Undecoded against a `capture` Source | Resolved the **byte-offset** way (`#87`), *not* the stream-offset way we recommended | The block's body is read by the referenced source's `kind`. Against `capture`: ids MUST be `0`, offsets are capture-file byte offsets, **only the bytes-exist class is available**, and it **discharges no coverage obligation and creates none** |
| **F4** — the unmarked-break check | Predicate written out normatively (`#88`) | Implement it verbatim; our guessed version in the review is what shipped |
| **F3** — mixed layers in one participant | Now a semantic violation (`#90`) | One check, and it licenses per-participant layer caching |
| **F9** — `content_type` at the transport layer | MUST NOT, **advisory** (`#95`) | `AdvisoryError` (we already have it, `errors.py:43`); harness needs an advisory path |
| **F6** — self-derivation | Detection stated as **partial by design** (`#93`) | Only checkable when a path is known; a reader handed a file object is conformant in accepting |
| **F7** — unanchored transport streams | Writer-only MUST NOT (`#94`) | **No reader can check it.** Record the omission as a decision, as upstream's `check.py` does |
| **F5** — the `skipped` vocabulary split | **Not adopted** | The filter's Discontinuity duty stays producer-knowledge-only. Record as a documented non-adoption |

Four vectors are new since the review was written and were not sized there:
`isolate-hole-against-capture`, `isolate-unbound-zpf-stream`,
`isolate-mixed-layer-participant`, `advisory-transport-content-type`.

---

## The first defect in `0.16`, found while reading it

*Reported upstream as
[zipline#103](https://github.com/adamkjonsson/zipline/issues/103). Later ones are
in the [register](#phase-8--prose-and-the-upstream-report).*

**§Layers still asserts the file-purity rule `0.15` replaced.** Line 741 of the
specification reads:

> A derived file is therefore exactly one of a *decode stage* or a *pass-through
> transform*, never a mix (see [Conformance](#conformance)).

§Conformance says the opposite, twice: *"The discriminator binds per participant,
so one file MAY do both"*, and `mixed-derivation` is an **accept**-tier vector.
The sentence is unchanged from `0.14`, where it was correct.

This is precisely the shape `0.16`'s new `RETIRED_CLAIMS` ratchet (`#100`) exists
to catch, and it survived for the reason that entry itself names: the ratchet
holds a retired claim from *returning* and cannot find one nobody has noticed.
The `#70` restatement grep misses it for the complementary reason — it is not a
copy of the per-participant rule but the *previous* rule, sharing no phrase with
its replacement. `#103` proposes the wording and the ratchet entry.

**It changes nothing about what we implement** — §Conformance and the vector
agree, and the offending sentence even points the reader at §Conformance. But an
implementer reading §Layers front-to-back builds the wrong classifier, which is
exactly what our `conformance.py` does today.

It was the first defect this port found and it is not the last. The running list
is the [register in Phase 8](#phase-8--prose-and-the-upstream-report), which is
also where each one gets reported upstream.

---

## Strategic decisions

### D1 — `is_decoded_stream` is removed, not repaired

`zpf.is_decoded_stream(records: Sequence[Record]) -> bool` (`reassembly.py:42`)
answers from `decoder_id` alone. Under `0.16` that is half the question, and the
other half needs the Decoder table, which a record sequence does not carry. The
signature cannot be fixed in place.

**Decision:** remove the free function; replace it with

```python
def stream_layer(records: Sequence[Record], decoders: Mapping[int, Decoder]) -> OutputLayer | int
```

and keep `SessionReader.is_decoded_stream(pid)` working by renaming it
`SessionReader.layer(pid) -> OutputLayer | int` (the reader has the table). An
unrecognised value is returned as the raw `int`, matching how we already carry an
unknown Source `kind`.

`record_ranges` and `stream_extent` sit directly downstream and take the layer as
an argument rather than recomputing it.

### D2 — `file_kind` is removed, replaced by a per-stream accessor

`FileReader.file_kind` returns `"raw" | "decode-stage" | "pass-through" | None`
(`reader.py:448`). All three words are wrong under `0.16` and the question is now
per-stream. Redefining it would keep a name whose answer is unsound.

**Decision:** remove it. Add

```python
def stream_kind(self, session_id: int, pid: int) -> tuple[Provenance, OutputLayer | int]
```

`cli.py:101` prints `kind:` today; it becomes a per-stream line. This is the
port's most visible public break and needs a `CHANGELOG` entry of our own.

### D3 — the merge's input bar is restated on the layer — **recommended, needs a call**

`_require_mergeable` (`transform.py:161`) rejects anything whose `file_kind` is
not `None`/`"raw"`. The property the merge actually needs is *every participant
is at the transport layer*; "raw" was a proxy for it that stopped being one.

**Decision (recommended):** bar on the layer. This is a **capability gain** — a
sessionization stage's output becomes mergeable, which the standard permits and
`0.14` could not express. It also stops rejecting a capture-sourced file whose
reassembler declared itself (`reassembler-declared`), which the current bar would
now reject for carrying a Decoder.

Flagging it because it widens what `merge` accepts rather than narrowing it, and
that is a product call, not a conformance one.

### D4 — the unmarked-break predicate lives in `conformance.py`, streaming

The predicate needs, per output participant: the previous record's per-input span
extents, plus the `hole`-class Undecoded regions seen so far. That is O(1) state
per live participant, freed at Session End — it does **not** need the whole file.

**Decision:** implement it in `ConformanceChecker`, not in `check_coverage`, and
keep it streaming. Gate it on the participant's layer being `decoded`, which is
the clause that keeps `sessionization-stage` and `tunnel/inner.zpf` passing.

### D5 — self-derivation is checked only when a path is known

`zpf.open()` accepts a file object, and the specification is explicit that such a
reader **is not obliged to detect** self-derivation. `FileReader` knows its path
only when opened from one.

**Decision:** compare a `zpf-input` Source's `uri` against the opened path after
normalisation, when a path exists; emit nothing when it does not. No new public
API. `isolate-self-derived` is judged by the harness with a path in hand.

### D6 — what a filter owes at a drop point is now settled

This was **D5 of the 0.14 plan**, left open and reported upstream as
[zipline#78](https://github.com/adamkjonsson/zipline/issues/78). `0.15` closed
it: a filter whose dropped records break its survivors apart **MUST** emit a
Discontinuity. `rewrite_decoded` implements it as required rather than optional,
with a declared `width` where the drop width is known (`filtered-decoded` ships
`width = 40`).

**Decision:** the decode-stage helper gains a **seam API** — the producer answers
*do these two join?* at each seam rather than remembering to emit a block. That
is the only way an ergonomic writer can enforce a duty resting on producer
knowledge.

---

## The surface, item by item

### New syntax — mechanical

**`blocks.py`** — `_DECODER_BODY` becomes `decoder_id: u16, output_layer: u8,
_reserved: u8`. New `OutputLayer(IntEnum)` with `DECODED = 0`, `TRANSPORT = 1`,
given the same load-bearing treatment as `SourceKind` (`blocks.py:103`): an
unrecognised value is preserved as a raw `int` through a round-trip, isolates
rather than being guessed, and is never defaulted. `Decoder(...)` defaults to
`DECODED` so existing writer calls keep working.

**`binary.py`** — no frame change. `SPEC_VERSION` moves to `(0, 16)`
(`blocks.py:42`); `0.14` and `0.15` files stop being readable, per the `0.x`
rule.

### JSONL face — `jsonl.py`

- `format` string → `zipline-payload/0.16`.
- `decoder` lines **always** carry `"output_layer"`, both directions, rendering
  `"decoded"` / `"transport"` and an unrecognised value as its raw number.
- Every golden `.jsonl` regenerates.

### Semantics — the real work

**`conformance.py`** is the largest change, and most of it is deletion.

*Goes:* `file_kind`, `_lock_kind`, `_require_derived`, `_require_derived_header`,
`_orphan_participants` — the whole file-purity machinery (`conformance.py:244`,
`615`–`670`). `CoverageLedger.findings(file_kind)` loses its argument.

*Arrives:* per-participant classification holding `(provenance, layer, created |
preserved)`, keyed by `(session_id, pid)`, freed at Session End — the same shape
as the per-session state we already carry.

*New rules*, each with its vector:

| Rule | Strength | Vector |
|---|---|---|
| Unknown `output_layer` | isolate | `isolate-unknown-output-layer` |
| A participant's records resolve to two layers | isolate | `isolate-mixed-layer-participant` |
| A `zpf`-sourced participant carries neither `origin` nor records with `spans` | isolate | `isolate-unbound-zpf-stream` |
| A `hole`-class Undecoded region against a `capture` Source | isolate | `isolate-hole-against-capture` |
| A file derives one of its own streams from another | isolate, **path-gated** | `isolate-self-derived` |
| `content_type` on a transport-layer record | **advisory — accepts** | `advisory-transport-content-type` |
| The unmarked-break predicate | isolate | `isolate-unmarked-break` |

*Changed rules:*

- Discontinuity is barred by **layer**, not by file kind.
  `isolate-discontinuity-in-raw` keeps its name and changes its reason.
- Undecoded is permitted on capture-sourced streams; the body is read by the
  referenced source's `kind`; against `capture` it is **excluded from the
  coverage ledger entirely** (it discharges nothing and creates nothing).
- `decoder_id` is permitted on capture-sourced records.
- The capture-sourced fast path **disappears**: "this file is raw" no longer
  retires coverage bookkeeping, because `mixed-derivation` exists. Live memory
  stays O(cited input streams).
- `transform_params_digest`'s bar is restated as *a file all of whose streams are
  capture-sourced* (`conformance.py:393`).
- `produced_by` / `produced_at` are no longer derived-only — `proxy-decoded` sets
  them on a file with no `zpf`-sourced stream.

**`reader.py` / `reassembly.py`** — D1 and D2, plus layer caching per
participant.

**`transform.py` / `decode.py` / `reassembly.py`** — the seam API (D6); the
filter's declared `width`; the reassembly helper may now declare itself as a
Decoder with `output_layer = transport` and emit Undecoded for discarded
overlaps; and a **new capability**, a sessionization stage over a `.zpf` input,
which `0.14` could not express at all.

### Harness — `tests/test_vectors.py`

- Re-vendor at `v0.16`; reset `KNOWN_PASSING` to empty, as Phase 0 of the `0.14`
  port did. The ratchet is a record of what we pass against *these* files.
- **`tunnel/`** is a four-file **accept** fixture. Our `chain/` path already
  handles multi-file accept; the new work is verifying each declared `digest`
  against the sibling it names — the manifest says they are real SHA-256s
  throughout, in both `chain/` and `tunnel/`.
- **The advisory tier.** `manifest.json` gains an optional `advisory: true` on an
  `accept` entry, which then declares **1** violation rather than 0. Our tier
  routing asserts `violations == 0` for accept today; it needs a third path that
  asserts *accepted, and diagnosed*. This is the one harness change that is not
  bookkeeping.

---

## Phases

Eight phases, each a PR, behind the existing vector ratchet — the same shape as
the `0.14` port. The suite stays green at every step.

### Phase 0 — re-vendor the suite
Copy `vectors/` at `v0.16`, update `VENDORED.md` (tag, commit, date, 53
directories), reset `KNOWN_PASSING`. Add defect 3 to `VECTOR-DEFECTS.md` — the
`undecoded-in-capture` byte change is the one defect there that was **not**
vector-side alone, since the text disagreed with itself.

### Phase 1 — version gate, Decoder body, `OutputLayer`
`SPEC_VERSION` → `(0, 16)`; the body split; the enum with `SourceKind`'s
treatment. Targets `reject-unknown-minor` and the round-trip property tests.

### Phase 2 — the JSONL face
`output_layer` on `decoder` lines both ways; format string; goldens regenerate.
Targets `test_the_shipped_jsonl_converts_back_to_the_binary` across all 53.

### Phase 3 — layer resolution and the reader break
D1 and D2. Per-participant caching. `record_ranges` / `stream_extent` take the
layer. Targets `sessionization-stage`, `reassembler-declared`, `proxy-decoded`,
`tunnel/`.

### Phase 4 — per-participant classification
Delete the file-purity machinery; build the per-participant classifier. Targets
`mixed-derivation`, `isolate-unbound-zpf-stream`,
`isolate-mixed-layer-participant`, and keeps every `0.14` isolate vector passing.

### Phase 5 — Undecoded against a capture, and coverage
The `kind`-keyed body reading, the bytes-exist-only rule, exclusion from the
ledger. Targets `undecoded-in-capture`, `isolate-hole-against-capture`, and the
four coverage vectors.

### Phase 6 — the Discontinuity predicate and the advisory tier
D4, plus `content_type`-at-transport as advisory, plus D5's path-gated
self-derivation check, plus the harness's advisory path. Targets
`isolate-unmarked-break`, `advisory-transport-content-type`,
`isolate-self-derived`, `filtered-decoded`, `reordered-decoded`.

### Phase 7 — the write side
D6's seam API; the filter's width; the reassembler declaration; the
sessionization stage. Round-trips every accept vector through our own writer.

### Phase 8 — prose, and the upstream report
Retire "raw" throughout `docs/`, `README.md`, `DECODER_API.md`,
`CONTENT_TYPE_API.md`. **Larger than a `sed`**: several passages explain *why*
raw and derived line up, which is the thing that stopped being true. Record the
F5 non-adoption and the F7 unenforceable rule in `docs/dev/conformance.md`, so
neither reads as an oversight in our checker.

**And file an issue upstream for every defect this port found**, on
[`adamkjonsson/zipline`](https://github.com/adamkjonsson/zipline/issues), one
issue per defect, in the house style of `#89`/`#91`: the quoted text with its
line citation, what contradicts it, why it survived, then a `## Fix` section
proposing wording. Where the defect is vector-side, say which artifact has to
change and why the text is not the thing that is wrong.

Batching them here rather than filing as we go is deliberate. A defect found at
Phase 1 is a guess about what the text means; the same defect after the rule is
implemented and the vectors are passing is a report backed by a working reader,
and that is worth more to the maintainer than being three days earlier. It also
lets one issue reference another where two turn out to share a cause, as `#89`
and `#91` did.

**The register.** Every defect goes in the table below as it is found, whichever
phase finds it, so nothing is left to be remembered at the end. A vector-side
defect is *also* recorded in `VECTOR-DEFECTS.md` and held in the harness's
`DEFECTIVE` — the issue is the report, that file is our record, and the two are
kept in step.

| # | Defect | Found | Kind | Upstream |
|---|---|---|---|---|
| 1 | §Layers still asserts a derived file is "never a mix", which §Conformance and `mixed-derivation` contradict | reading `0.16` | text | **filed**, [#103](https://github.com/adamkjonsson/zipline/issues/103) |
| 2 | `tunnel/{inner,outer}.jsonl` spell the flow key `flow_key`, where the JSONL mapping's alias table says `key` and `descriptive-metadata.jsonl` writes `key`. The tunnel walkthrough (spec `:1183`, `:1194`) has the same slip | Phase 1 | vector + text | **filed**, [#104](https://github.com/adamkjonsson/zipline/issues/104) |

Phase 8 is done when every row reads **filed** and carries its issue number.

---

## What this does not change

- **The frame.** No block-type, no length rule, no option framing change.
- **The hot read path.** No new block, no new record option, no change to record
  framing or payload handling. `output_layer` costs one `u8` per Decoder
  Descriptor.
- **Single-pass streaming.** Decoders are subject to declare-on-first-use, so the
  layer table is populated before any record references it. No buffering, no
  second pass, no back-patching.
- **The merge algorithm**, `order.py`, and cross-file splice checking —
  `check_splice` needs no new state, because a Discontinuity still carries no
  offset field and its position still comes from stored order.
- **`sequenced_basis`**, which stays conditional; our documented non-adoption of
  the split variant stands.

---

## Sizing

Phases 1, 2 and 5 are a day between them. Phase 3 is a day, most of it in the
public-API break and its docs. Phase 4 is a day, and is mostly deletion. Phase 6
is the hard one — the predicate is small but every near-miss vector has to be
verified against it individually — call it two days. Phase 7 is two days,
because the seam API is a new user-facing concept rather than a rule. Phase 8 is
a day and a half: a day of prose, and half a day for the upstream issues, which
is the register's length times the half-hour a well-evidenced issue takes.

Call it **eight to ten working days**, with Phase 6 the one that can overrun.

Nothing here is blocked. `0.16` resolved both findings that would have stopped
us, which is the whole reason to port to it rather than to `0.15`.

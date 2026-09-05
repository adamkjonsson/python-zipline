# Impact of the spec simplification on `python-zipline`

> **Assessment, not a plan.** Written 2026-09-03 against
> [SIMPLIFICATION-ANALYSIS.md](https://github.com/adamkjonsson/zipline/blob/main/docs/SIMPLIFICATION-ANALYSIS.md)
> in the spec repository, which ranks five principles of the `0.18` format that
> could be loosened. This document asks what each loosening would remove from
> *this* library, what it would cost us, and what it would do to the
> [`v0.3.0` plan](V0.3.0-RELEASE-PLAN.md). It decides nothing; the direction is
> the spec's to choose. Line counts throughout are estimates from reading the
> code, not measurements.
>
> **Answered 2026-09-05, and this document is now historical.** `0.19` took
> exactly the three packages it supports — 3.1, 3.2 and 3.3 — and declined 3.4
> and 3.5. The spec repository replied to it in two messages, filed here as
> [#64](https://github.com/adamkjonsson/python-zipline/issues/64) and
> [#65](https://github.com/adamkjonsson/python-zipline/issues/65), which name
> four things this assessment got wrong or missed: three vectors carrying
> `origin` that §2's 3.1 entry does not list, the merge coverage obligation
> being unconditional rather than conditional on 3.4, the `0.18` re-issue's
> findability goal settling 3.4 in the middle path's favour, and transforming
> decoders being a gap we could not price. The consequences are worked through
> in the [`v0.3.0` plan](V0.3.0-RELEASE-PLAN.md), which is the live document;
> §4 of this one, on what the packages would do to that plan, is superseded by
> it.

The library is at `0.2.0`, a complete implementation of spec `0.16`. The
`v0.3.0` plan is written against `0.17`. The spec repository is at `0.18`, cut
2026-09-02, and the simplification would land at `0.19` or later. So there are
already two unported spec versions ahead of us, and the question this document
ends on is whether to port through them or wait.

---

## 1. Verdict

**The package would cut the spec roughly in half but this library by about an
eighth of its source.** The spec's weight is in normative prose about
derivation. Ours is in mechanism the proposals never touch: the frame, the block
table, both faces, reassembly, the writer ergonomics, the causal merge. What the
package removes here is concentrated in two modules,
[`conformance.py`](../src/zpf/conformance.py) and the derivation half of
[`transform.py`](../src/zpf/transform.py), plus the options they check.

| Area | Lines today | Roughly removed by 3.1–3.4 | By 3.5 on top |
| --- | ---: | ---: | ---: |
| `src/zpf/` | 10 134 | ~900 | ~300 more |
| `docs/` (user + dev) | 3 627 | ~700 | ~150 more |
| Tests (461 functions) | — | ~90 touched, most rewritten rather than deleted | ~50 more |
| Vectors (64 at `0.18`) | — | 18 removed, 4 rewritten | 8 more |

**The real gain for this project is not lines. It is the end of the churn.**
Two facts from our own history make that concrete:

- **Every finding we filed on `0.18` falls inside 3.3 or 3.4.** Of
  [#113](https://github.com/adamkjonsson/zipline/issues/113),
  [#114](https://github.com/adamkjonsson/zipline/issues/114),
  [#115](https://github.com/adamkjonsson/zipline/issues/115),
  [#116](https://github.com/adamkjonsson/zipline/issues/116) and
  [#118](https://github.com/adamkjonsson/zipline/issues/118), all five are the
  advisory tier's pinned repairs;
  [#117](https://github.com/adamkjonsson/zipline/issues/117) is the `dropped`
  word inside the coverage apparatus. Every one closes by deletion.
- **Each port since `0.14` has cost a plan and a review round**, and the deltas
  they carried — `output_layer`, the created/preserved discriminator, the seam
  predicate, `input_extents`, `reason_class`, the origin floor, `dropped` — are
  all in the clusters the analysis names. What survived those ports untouched is
  the part of the format the analysis does not propose to change.

The corollary for the release in flight: **Phase 4 and half of Phase 5 of the
`v0.3.0` plan build things 3.3 and 3.4 would then remove.** See §4.

---

## 2. Per proposal

Ordered as the analysis orders them. Each entry names the code that leaves,
what the library gives up, and what it gains.

### 3.1 Pass-through as a distinct derivation kind → every derived record carries `spans`

**What leaves.**

- In `conformance.py`: `_ParticipantState.origin_source` and `has_spans`, the
  origin handling in `_on_participant`, and the created/preserved rules in
  `_check_participant` ("carries origin and holds records carrying spans",
  "zpf-sourced but carries neither", "carries origin but its records are
  capture-sourced"). The `CoverageLedger.findings` gate "a pass-through cites
  nothing" becomes unnecessary, since every `zpf`-sourced stream now cites.
- In `transform.py`: `_copy_participant` stops writing an `Origin`; `_reissued`
  stops stripping spans and writes an identity span instead; `resolve_spans`,
  `_resolve_at` and `_sibling_opener` — the two-hop walk, about 110 lines —
  collapse to "return the record's spans".
- `Origin` in `blocks.py`, its JSONL projection, `participant(origin=)` on the
  writer, and the `origin` column of `FileReader`'s index.
- Docs: `concepts.md` "Created or preserved" (~60 lines), `provenance.md` "Two
  ways bytes are traced" and "Following the chain", `howto/merge.md`'s
  description of the output, and the second trap in `CLAUDE.md`, which reduces
  to one sentence.
- Vectors: `passthrough-transport`, `passthrough-discontinuity`,
  `annotator-decoded`, `isolate-unbound-zpf-stream`; `mixed-derivation` and
  `merge-timestamp-tie` are re-vendored with identity spans.

**Give up.** Nothing we use. `merge_files`'s promise that records are
"re-emitted byte-identically" stops being literally true — each grows by a
28-byte span — but nothing downstream relies on that. zpfwire does not write
pass-throughs.

**Gain.** One answer to "where did this record come from": `record.spans`,
always, one hop. The asymmetry the checker's module docstring spends a paragraph
on — inherited Undecoded blocks copied verbatim while Discontinuities are
renumbered — goes with it.

**An interaction the analysis does not name.** A merge output carrying spans now
*cites* an input stream, and under the current coverage MUST that makes the file
answerable for it: every offset of the input must be spanned or marked. A
transport input's holes are real ranges no payload covers, so `merge_files`
would owe an Undecoded `gap` block per hole. That is easy — the merge already
reassembles and `_extent_and_gaps` exists — but it is new work in the merge if
3.1 is taken **without** 3.4. With 3.4 it is a SHOULD we would honour anyway.

### 3.2 A producer must justify its sequencing claim → `SEQUENCED` is a bare assertion

**What leaves.**

- `_derive_sequenced_basis` and `_has_ordering_hints` in `writer.py`
  ([`:200`](../src/zpf/writer.py#L200), ~45 lines), the basis inheritance in
  `derive_from`, `sequenced_basis=` on `begin_session`, `single_clock=` on
  `create`/`FileWriter`, and `FileFlags`.
- `_check_sequenced_basis` in the checker, `_SessionState.sequenced_basis` and
  `has_hints`, and the first of `finish()`'s two reasons to exist — the
  hint-less question is only decidable at Session End.
- `SEQUENCED_BASES`, `Session.sequenced_basis`, `FileHeader.flags` and
  `single_clock` in `blocks.py`; the `single_clock` JSONL alias; the
  "single-clock" note in `zpf info`.
- Docs: the basis section of `guides/ordering.md`, the `SINGLE_CLOCK`
  discussion in `concepts.md`.
- Vectors: `sequenced-basis`, `isolate-sequenced-no-basis`,
  `partially-hinted-sequenced`, `file-clock-metadata`.

**Give up.** A guard, not a feature. Today `derive_from` **refuses** to mark an
output session sequenced when nothing about the input justifies it
([`writer.py:236`](../src/zpf/writer.py#L236)): "naming a basis that is not true
is worse than refusing: a reader may act on the flag". Under 3.2 that refusal
has no rule to stand on, and a derived stage asserts `SEQUENCED` on the same
trust the input's producer had. `verify_sequenced` is unaffected: it checks the
*order*, which is a different rule.

**Gain.** `derive_from` loses its only failure mode. zpfwire drops two keyword
arguments. The checker's Session End phase shrinks to the per-participant rules.

### 3.3 Two readers must agree on non-conformant input → one treatment per violation

**What leaves.** Almost nothing that exists today. The advisory *mechanism* —
`AdvisoryError`, `_note`, the two-phase `observe()`, `_admit`'s keep-and-report
path in the reader — **stays**, because the loosened rule still says "ignore the
offending option, with a diagnostic". What goes is the *pinned repair*: the
running-maximum placement of an unplaceable record, the "not evidence of layer"
clause for a transport `content_type`/`role`, and the handshake placement
paragraphs. In this repo that is docstrings, plus the four `advisory-*` vectors.

What it removes from the **plan** is larger than what it removes from the code:
Phase 4's `seq-before-origin` and `syn-seq-start` advisory checks are never
built, and Phase 1's "reconcile current end with `0.17`'s words" item and its
issue ([#114](https://github.com/adamkjonsson/zipline/issues/114)) become moot.

**Give up.** Two readers agreeing on `stream_extent` and `record_ranges` for a
malformed file. That is exactly the disagreement behind
[#63](https://github.com/adamkjonsson/python-zipline/issues/63): zpfwire wrote a
SYN one below the origin, and our reader and kober's produced different numbers
from it. Under 3.3 that disagreement is licensed. The mitigation is the
**write-side guard** in Phase 4, which stops the file existing and survives
every package — the `isn + 1` MUST is in §Record and 3.3 deletes only its
advisory sentences.

**Gain.** We stop litigating placement. But note the honest arithmetic: 3.3
saves spec text, not reader code. `record_ranges` still has to put an
unplaceable record *somewhere* — one range per record, index-matched to
`blocks`, is a promise our API makes — so Phase 1's `_offset_of` fix is needed
under every package. What changes is that our choice is ours, undocumented
upstream and unvectored.

### 3.4 Coverage as a verifiable MUST → a SHOULD, no checker proves it

**What leaves.** The biggest code item, and it is one coherent apparatus:

- `CoverageLedger` and `_StreamLedger` (~130 lines), `coverage_findings`, the
  `extents-disagree` / `extent-exceeds-coverage` / `extent-below-coverage`
  categories, and the second of `finish()`'s two reasons to exist.
- The single-file seam predicate: `_BreakCandidate`, `_track_break_candidates`,
  `_check_unmarked_breaks`, `_holes`, `_breaks`, and `prev_reach` /
  `broke_since` / `candidates` on `_ParticipantState` (~100 lines).
- `_check_reason_class`, `_reason_class`, `REASON_CLASSES`, `reason_class` on
  `Undecoded` and the writer, the `dropped` reason.
- `check_extents`, `_declared_extents`, `_check_declared` in `transform.py`;
  `_declare_extents` in `DecodeStage`; `InputExtent` and `input_extents` on
  `SessionEnd` and `SessionWriter.end`.
- Docs: `howto/validate.md`'s file-alone section, the `input_extents` half of
  `guides/decoding.md` "Coverage is handled for you", the `reason_class`
  paragraphs of `guides/provenance.md`, and most of `dev/conformance.md`'s
  "Coverage is settled in two places".
- Vectors: `isolate-coverage-gap`, `isolate-extent-exceeds-coverage`,
  `isolate-extents-disagree`, `isolate-unmarked-break`, `isolate-unmarked-drop`,
  `undecoded-reason-class`.

**What stays.** `check_coverage` against the input — it needs a second file and
a SHOULD is still worth linting. `DecodeStage`'s auto-fill, `Seam`,
`discontinuity()`, `rewrite_decoded`'s `_declare_seam` and `_inherited_breaks`:
these are how *our* output honours the SHOULD by construction, and nothing about
a SHOULD says stop. `check_splice`, which the analysis is right to say is about
reading rather than verification.

**Give up.** The one real capability loss in the package, and it lands on the
read side. `zpf validate FILE` with no `--input` stops being able to say that a
decode stage dropped nothing; the answer now needs the input in hand. Our own
producers are unaffected, since auto-fill keeps their output complete. What we
lose is verifying **other** producers' files, kober's above all — and kober is
the producer whose captures found #58, #62 and #63.

**Gain.** `conformance.py` loses roughly a third of its length, and with 3.2
also taken the checker has **no end-of-stream phase at all**: it becomes the
single-pass observer its docstring describes, and "consistent-on-raise" is
trivially true. The `dropped` work in Phase 5 — the one item in the plan that
turns a green vector red — is never done, and
[#117](https://github.com/adamkjonsson/zipline/issues/117) closes by deletion.

**A middle path worth raising upstream.** Keep the coverage MUST; delete only
the single-file *verification* apparatus — `input_extents`, `reason_class`,
`dropped`, the seam predicate. The property "nothing vanishes silently" then
holds for a **pair** of files rather than for one, which is already how the
splice duty is stated and checked. That keeps `check_coverage` a conformance
check rather than a lint, keeps our producers honest by construction, and still
removes everything the review rounds tripped on. The format already carries
MUSTs no single file can verify — the transport-withholding rule is one — so
this is not a new kind of rule.

### 3.5 Provenance and layer as independent axes → the `0.14` model

**What leaves.**

- `OutputLayer`, `Decoder.output_layer`, `stream_layer` and `layer_name` in
  `reassembly.py`, the `layer` parameter threaded through `record_ranges`,
  `stream_extent` and every caller; `SessionReader.layer`, `FileReader.stream_kind`.
- In the checker: the mixed-layer rule, the unknown-layer rule, the
  layer-keyed Discontinuity rule (re-keyed on `decoder_id`),
  `_check_against_capture`, and the capture branch of `CoverageLedger.observe`.
- `Hints` on `DecodeStage.record` — a decode stage can no longer emit a
  transport layer, so a stage has no use for `seq_start`/`ack`. (This helps
  Phase 3's argument count: 11 → 10 before merging `cites` and `spans`.)
- `_require_mergeable` reverts from "at the transport layer" to "carries no
  `decoder_id`".
- Docs: `concepts.md` "Two axes" (~40 lines), the first trap in `CLAUDE.md`
  reverses, and `stream_kind` leaves the tutorial and `zpf info`.
- Vectors: `tunnel/*`, `sessionization-stage`, `reassembler-declared`,
  `proxy-decoded`, `undecoded-in-capture`, `isolate-mixed-layer-participant`,
  `isolate-unknown-output-layer`, `isolate-hole-against-capture`.

**Give up.** Something this family asked for. zpfwire declares itself as a
reassembler with `output_layer=TRANSPORT` and marks the overlaps it discards
with capture-sourced Undecoded blocks
([`convert.py:161`](https://github.com/adamkjonsson/python-zipline-wire/blob/main/src/zpfwire/convert.py),
[`flow.py:165`](https://github.com/adamkjonsson/python-zipline-wire/blob/main/src/zpfwire/flow.py)).
Both go, and with them the only place a reassembler can record a
`params_digest`, since a capture-sourced file MUST NOT carry
`transform_params_digest`. The tunnel per-hop account goes too, which matters
if kober ever decodes through TLS.

**Gain.** The largest *reader-side* removal, in the two most-used modules. But
it is also the highest-churn item: `record_ranges` and `stream_extent` change
signature, so every caller follows, and the code's own history records that the
`decoder_id`-alone rule was replaced because "its signature could not be
repaired". Reversing it is a real port, not a deletion.

**Position.** Against, agreeing with the analysis's ranking, and for a stronger
reason than "tunnels may be rare": the request that made `0.15` came from the
producer this library is built beside.

### Rationale extraction

Zero code impact, and the largest spec win. One observation for us: our own
docs and docstrings carry the same habit. The `conformance.py` module docstring
is 60 lines, most of it the argument that produced the rules; `concepts.md` and
`guides/provenance.md` retell the spec's rationale rather than the library's
usage. The same extraction discipline would apply here independently of any
format change, and is not a `0.x` question.

---

## 3. Two interactions the analysis does not name

1. **3.1 without 3.4 makes the merge owe coverage.** Stated under 3.1 above. A
   real but small piece of work, and one the analysis's "cost is 28 bytes per
   record" understates.
2. **3.2 and 3.4 together remove `finish()`.** Each alone leaves the checker an
   end-of-stream phase for the other's reason. Together they leave only
   `_close_participants` for sessions that never got a Session End, and under
   3.1 as well that method has almost nothing left to rule on. The
   "bounded memory" and "consistent-on-raise" design points in
   `dev/conformance.md` become properties of a single-pass observer rather than
   things to preserve when editing.

---

## 4. What it means for `v0.3.0`

The plan's phases sort cleanly by whether the package touches them.

| Phase | Under 3.1–3.3 | Under 3.4 as well | Verdict |
| --- | --- | --- | --- |
| 1 — one offset rule | The fix survives; the "reconcile with `0.17`'s words" item and the spec citation in the docstring go | unchanged | **Do the fix.** Hold the citation half |
| 2 — timestamp API | untouched | untouched | **Safe** |
| 3 — signature restructure | `Decoded(decoder, content_type, spans)` unchanged; `Hints` on `SessionWriter.record` unchanged | untouched (3.5 would remove `hints=` from `DecodeStage.record`) | **Safe** |
| 4 — advisory checks | The two read-side checks are never built. The **write-side guard** survives every package | unchanged | **Hold the checks; do the guard** |
| 5 — port to `0.17` | The port itself is fine; `advisory-seq-start-below-origin` leaves `KNOWN_PASSING` at the next re-vendor | The whole `dropped` item — `UNDECODED_REASONS`, the predicate's second arm, `isolate-unmarked-drop` — is built and then deleted | **Hold `dropped`** |
| 6 — `role` | untouched; `role` survives every package | untouched | **Safe** |

Two consequences for the plan's own text:

- **The `KNOWN_PASSING` ratchet resets at any re-vendor that removes vectors.**
  The rule "a name is never removed" is about our behaviour against a fixed
  vendoring; when upstream deletes the file, the name has nothing to guard. The
  `0.16` port already did this at Phase 0 and the harness docstring records it.
  Twelve names would leave under 3.1–3.3, eighteen with 3.4.
- **Porting through `0.17` and `0.18` to reach `0.19` means implementing the
  deleted rules once each.** Concretely: the `dropped` arm, the running-maximum
  placement rule, the syn advisory in both directions, the `advisory-*` vectors,
  and `advisory-transport-role`. None of that is large — perhaps two days across
  Phases 4 and 5 — but all of it is throwaway if the package lands, and the plan
  sequences it **first**.

**The decision the analysis forces on this repo** is therefore not which
proposals to support. It is whether `0.3.0` ports to `0.18` now or waits for the
simplified release. Two readings:

- **Port now.** `0.3.0` ships Phases 1–6 against `0.18` as planned, and `0.4.0`
  ports to the simplified spec by deletion. Costs the throwaway above; delivers
  `role` and the #62/#63 fixes to kober and zpfwire months earlier.
- **Wait.** `0.3.0` ships Phases 1, 2, 3 and the Phase 4 write guard against
  `0.16` — all wire-compatible fixes — and the port becomes one step to the
  simplified spec, carrying `role` with it. Costs kober `role` until then;
  saves the throwaway and one migration plan.

The plan's own argument for porting first — "Phase 4's guard cites a MUST that
exists, Phase 6 has an option to emit" — holds under either reading, since both
the `isn + 1` MUST and `role` survive every package. What no longer holds is
"front-loads the `dropped` breakage", because under 3.4 there is no `dropped`.

---

## 5. Recommendation

On the proposals, as seen from this implementation:

- **3.1, 3.2, 3.3: support**, with the two amendments in §3. The package loses
  us nothing we exercise, and it closes every issue we have open upstream.
- **3.4: support the middle path** in §2 — a pair-verifiable MUST rather than a
  SHOULD — and if the spec prefers the clean SHOULD, accept it. The read-side
  loss is real for kober's consumers, but every producer in this family already
  honours coverage by construction, and the verification apparatus is where the
  last three review rounds were spent.
- **3.5: oppose.** It reverses zpfwire's declared shape and takes the
  reassembler's `params_digest` with it.

On sequencing here: do not build Phase 4's read-side checks or Phase 5's
`dropped` item until the spec direction is chosen. Phases 1, 2, 3 and 6 and the
Phase 4 write guard are safe under every outcome and can proceed now.

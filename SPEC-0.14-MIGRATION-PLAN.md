# Migration plan: `python-zipline` 0.12 → 0.14

Target: [Zipline Payload Format 0.14](https://github.com/adamkjonsson/zipline/blob/v0.14/docs/zipline-payload-format.md)
(tag `v0.14`). Current: 0.12.

## What we are actually migrating to

Two spec releases separate us from the target, and only one of them adds
anything:

| Release | Nature | Work for us |
|---------|--------|-------------|
| `0.13` | **Additive.** One new block (`Discontinuity`), three new options, two clarifications the vectors had been contradicting | Essentially all of it |
| `0.14` | Corrective. Fixes all seven findings of our own [`0.13` review](SPEC-0.13-REVIEW.md), adds no option and no block | Settles three questions our review left open, and *tightens* two rules |

So this is a **0.12 → 0.13 port under `0.14`'s corrected reading**. `0.14` is not
extra surface; it is the answer to the questions the review said had to be
settled before anyone implemented. Every one of our seven findings was adopted,
including the three that decide how our code has to be written:

- finding 1 → the decoded offset space is defined **once**, and it counts
  declared `width`s. That decides whether `record_ranges()` sums widths. **It
  does.**
- finding 2 → a pass-through **renumbers** Discontinuity ids. §Discontinuity was
  the wrong copy and is now the only copy.
- finding 3 → the splice duty is a **MUST NOT**, on the consumer *and* on the
  next decode stage. So our reader does owe a duty at the join.
- finding 6 → coverage is **at least once**, not exactly once. Span-on-span
  overlap is legal, which is what our checker already assumed.

Two entries are filed under *Changed* rather than *Clarified*, and the CHANGELOG
is explicit that a reader conformant under `0.13` may not be under `0.14`: the
splice MUST NOT, and the `input_extents` SHOULD. Both land on us as new work
rather than as adjustments, since we never implemented `0.13`.

`0.14` files stamp `0`/`14`. As always, every minor is a separate format: files
this library has already written stay unreadable by it, and there is no
transcode path — `0.14` now says so in the specification itself rather than only
in the CHANGELOG, and records the rejected re-stamp option under *Design
decisions not taken*.

---

## One defect in the specification, found while reading it

**§File Header's field table says `version_minor` is `13` for this document.**

> \| `version_minor` \| u16 \| `13` for this document \|

Everything else in the release says `14`: the document title, the worked hex
dump (`000E  0E 00  version_minor = 14`), the CHANGELOG, and every shipped
vector — `raw-minimal` stamps `0E`, and `reject-unknown-minor` stamps `15`,
which is derived from the current version precisely so it keeps testing an
unimplemented minor. The table is a stale copy from `0.13`, and it is the same
fault this release exists to fix: a rule stated in more than one place with only
some copies updated.

**We implement `14`.** No ambiguity in practice, but it is worth reporting — the
field table is where a reader author looks first, and a reader built from it
would reject the entire vector suite. Filed for a `SPEC-0.14-REVIEW.md` batch.

---

## Strategic decisions

Five need a call. My recommendation is on each; D1 and D5 are the two I would
not proceed on without a nod.

### D1 — `record_ranges()` takes the participant's *blocks*, not its records

The positional rule is now

```
[ Σ(preceding payload_len + preceding declared widths), + its own payload_len )
```

counting that participant's records **and its Discontinuity blocks** in stored
order. Our `record_ranges(participant, records)` cannot express that: the widths
live in blocks it is not given, and their *positions* — which records they fall
between — are what makes them terms.

Three shapes:

| | Shape | Cost |
|---|---|---|
| (a) | `record_ranges(participant, records, discontinuities=[(index, block), …])` | Keeps the signature; callers must build a parallel index that is easy to get off by one |
| (b) | `record_ranges(participant, blocks)` where `blocks` is `Sequence[Record \| Discontinuity]` | Breaking; mirrors the spec sentence exactly |
| (c) | Leave it, add `decoded_ranges()` beside it | Two functions, one of which is silently wrong on any file with a width |

**Recommend (b).** The specification defines the space by walking one interleaved
sequence, and a function that takes that sequence cannot be called wrongly. It
returns one range per `Record`, so the return type is unchanged. (c) is the one
to avoid: a `record_ranges` that stays correct only for files without the new
block is exactly the "safe to skip" trap the release spends a page on.

`stream_extent()` and `SessionReader.ranges()` follow. `SessionReader` already
indexes blocks per participant, so feeding it the interleaved sequence is
internal.

### D2 — the splice check is a new pairwise function, not part of `check_coverage`

The MUST NOT is a property of a **pair** of files: stage 1 declares the break,
stage 2's `spans` cross it. The `splice/` fixture is built to make that
unavoidable — `http.zpf` is well-framed, fully covered, and has nothing wrong on
its face, so a harness that tests files individually passes it.

**Recommend** `zpf.check_splice(stage2, stage1) -> list[Diagnostic]`, symmetric
with the existing `check_coverage(decoded, raw)`, category
`"discontinuity-splice"`. Reusing `check_coverage` would mean a coverage
function that returns non-coverage findings, and the spec is emphatic that a
Discontinuity discharges no coverage obligation.

### D3 — standalone extent checking is a new function; `check_coverage` keeps its input

`input_extents` is what lets a consumer check the coverage guarantee **from the
file alone**, which our `check_coverage` cannot do — it requires the input file
to measure the streams.

**Recommend** adding `zpf.check_extents(derived) -> list[Diagnostic]`, which
needs only the derived file and reports:

- `"extent-exceeds-coverage"` — declared extent beyond what spans plus Undecoded
  account for (`isolate-extent-exceeds-coverage`, the silent-truncation case);
- `"extent-below-coverage"` — declared extent smaller than the covered union,
  the contradiction the spec names alongside it;
- `"extents-disagree"` — two sessions declaring different extents for one input
  stream (`isolate-extents-disagree`).

and keeping `check_coverage` as the stronger check when the input is in hand,
extended to cross-check declared extents against measured ones.

**The union must be keyed on the input stream `(source_id, session_id, pid)`,
never on the output session.** Ours already is, which the review flagged as luck
rather than design — `session-fan-out` is the vector that turns it into design,
and it is the only file in the suite that would catch the mistake.

### D4 — `external_session_id` is `bytes`, and stays `bytes`

First `bytes`-typed option in the registry. `Session.external_session_id:
bytes | None`, projecting to base64 under the same rule as `payload`. The spec
adds an explicit "a reader MUST NOT assume a `bytes` option is text, even when it
decodes to printable ASCII", so we must not offer a `str` convenience accessor
that would quietly re-spell a UUID. No decision really — recorded so it is not
re-opened.

### D5 — what `rewrite_decoded` owes at a drop point — **needs a call**

This is the one place the standard does not answer, and where an obvious
implementation goes beyond it.

`rewrite_decoded` is a decode stage that filters and reorders. Two duties are
clear from `0.14`:

1. It reads an input that may carry Discontinuity blocks, so the MUST NOT applies
   in full: it may not emit a unit spanning one without emitting its own in the
   corresponding position. Since it re-emits records rather than merging them, it
   satisfies this by **carrying every input Discontinuity forward, renumbered to
   its own ids** — the same mechanic as a pass-through, for a different reason.
2. It must not drop one. A declared `width` is a term in the arithmetic.

What the standard does **not** say is what a *filter* owes at a drop point.
Dropping a record and marking its range `skipped` leaves the two surviving
neighbours adjacent in the output offset space when they were not adjacent in the
input's — which is precisely the shape of the defect the Discontinuity block
exists to prevent, one hop along. But no rule requires a block there: the MUST NOT
is written about spans *crossing* an input Discontinuity, and a filter's spans
cross nothing.

**Recommend emitting one** — `width` absent, `reason` from an open vocabulary —
at every drop point and at every point where a reorder separates records that
were adjacent, and **saying plainly in the docstring that this exceeds what
`0.14` requires**. The alternative is a filtered file whose consumer splices two
messages that never adjoined, with the library having had the information and
said nothing.

Because this goes beyond the standard, it should be a parameter rather than
unconditional behaviour: `mark_gaps: bool = True`. Flagging it here rather than
deciding it.

---

## The surface, item by item

### New syntax — mechanical

| What | Where | Notes |
|---|---|---|
| `BT_DISCONTINUITY = 0x22` | `_frame.py` | Body `session_id: u64, participant_id: u16, _reserved: u16` — 12 bytes, new `struct.Struct("<QHH")` |
| `OPT_TRANSFORM_PARAMS_DIGEST = 0x0015` | `_frame.py`, `blocks.FileHeader` | string, single-valued |
| `OPT_EXTERNAL_SESSION_ID = 0x0054` | `_frame.py`, `blocks.Session` | **bytes**, single-valued |
| `OPT_INPUT_EXTENTS = 0x00C1` | `_frame.py`, `blocks.SessionEnd` | packed, **repeatable**, 20-byte entries |
| `OPT_WIDTH = 0x00D0` | `_frame.py`, `blocks.Discontinuity` | u64, optional; **absent means unknown** |
| `OPT_DISCONTINUITY_REASON = 0x00D1` | `_frame.py`, `blocks.Discontinuity` | string, open vocabulary |
| `INPUT_EXTENT_ENTRY = struct.Struct("<HHQQ")` | `_frame.py` | size 20; `MAX_EXTENTS_PER_OPTION = 65535 // 20 = 3276` |

`Discontinuity` is a new frozen dataclass in `blocks.py` alongside `Undecoded`,
registered in `parse_block`'s dispatch, and exported from `zpf/__init__.py`. An
`InputExtent` NamedTuple sits beside `Span`.

The repeatable-id closed list becomes `{endpoint, spans, input_extents}`;
`input_extents` chunks exactly as `spans` does (`mode="concat"` in our
`_OptSpec` table already covers it).

**`width` must be tri-state.** `None` is not a default of `0` — absent means
*unknowable extent* and contributes `0` to the arithmetic, while a declared `0`
would be a zero-width hole. They project differently in JSONL (omitted key vs.
`"width": 0`) and our round-trip guarantee depends on keeping them apart.

### JSONL face — `jsonl.py`

- `"discontinuity"` in the type table, both directions (`_enc_discontinuity` /
  `_dec_discontinuity`).
- `input_extents` → array of `{source_id, session_id, pid, extent}`, **always an
  array**, chunks merged on the way in and splittable on the way out — the
  `spans` rule, reused.
- `extent` and `width` join the 64-bit set that MAY be a decimal string and MUST
  be accepted as either.
- A `bytes`-typed option projects as base64. This is the rule we already apply to
  `payload` and Custom bodies; it now needs to be reachable from the option
  table rather than hard-coded per field.

### Semantics — the real work

**Positional arithmetic** (`reassembly.py`, `reader.py`). Per D1. The docstring
of `record_ranges` currently states the *old* rule in prose — "It is **not**
hole-inclusive" — which `0.14` explicitly calls false as an absolute. Replace
with: hole-inclusive only where a Discontinuity declares a width.

**Coverage** (`transform.py`). "At least once, never both." Our `_check_stream`
already flags only span-∩-Undecoded and never span-∩-span, so **no code change**
— but it becomes a load-bearing property rather than an accident, and needs a
test that locks it (`session-fan-out` has a legal `[0,80)` overlap and is the
only file in the suite exercising it).

**Fan-out** (`transform.py`, `conformance.py`). Output sessions need not mirror
input sessions in either direction, and a stage may mint sessions with no
upstream counterpart. Verify nothing in the conformance checker assumes
otherwise; the coverage keying is already right (D3).

**The splice duty** (new checker, D2). Plus the reader-side half: a consumer MUST
NOT treat records either side as contiguous. Concretely that means
`SessionReader.reassemble()` must not silently concatenate across a
Discontinuity — the `StreamView` needs to surface the break the way it already
surfaces a transport `Gap`, or the library itself commits the violation it is
checking others for.

**Raw-file prohibitions** (`conformance.py`). A raw file MUST NOT carry a
Discontinuity (`isolate-discontinuity-in-raw`), `input_extents`, or
`transform_params_digest`. Our `_lock_kind`/`_require_derived` machinery already
has the shape for all three.

**Correspondence, not identity** (`transform.py`, `decode.py`, docs). We never
asserted payload-to-span identity, so nothing breaks — but two things need
saying, and one needs a guard removed if we have one: a record of 8 bytes MAY
span 16, and `resolve_spans`' walk reaches the *corresponding* bytes, not
necessarily the ones it set out to find. The recoverability-walk docstrings say
"the actual bytes" today; `0.14` says "the bytes of the region it arrived at",
which is a different promise.

**Writer support**. `SessionWriter.discontinuity(pid, *, width=None, reason=None)`,
`SessionWriter.end(..., input_extents=[…])`, `FileWriter` accepting
`transform_params_digest`, `begin_session(..., external_session_id=…)`. On
`DecodeStage`, a `.discontinuity()` that records the break and an autofill for
`input_extents` at close — the stage knows every input stream's extent by then,
and `0.14` says it **SHOULD** declare it.

---

## Phases

Each phase is done when its vectors pass. The ratchet from the `0.12` port
carries over: `KNOWN_PASSING` only grows, and the suite is green at every step.

### Phase 0 — re-vendor the suite

Replace `tests/vectors/` with all **39** entries from `v0.14` (13 new: the six
`0.13` additions, the four `0.14` capability-coverage vectors, `session-fan-out`,
`isolate-extents-disagree`, `passthrough-discontinuity`, and the `splice/`
fixture). Update `VENDORED.md` to the new tag and commit.

**`VECTOR-DEFECTS.md` closes.** Both defects we reported were fixed upstream —
defect 1 in `0.13`'s *Fixed* (the three decode-stage vectors now set
`produced_by`/`produced_at`), defect 2 in the vectors README, which now reads
"Rejecting an `isolate` vector, with a diagnostic, **is conformant**". Rewrite
the file as a closed record rather than deleting it, and empty `DEFECTIVE` in the
harness.

Reset the ratchet: everything `xfail` except the five `reject-*` cases, which
pass on the version gate alone.

### Phase 1 — version gate and syntax

`SPEC_VERSION = (0, 14)`, the `_frame.py` constants, the `Discontinuity`
dataclass, the four new options, byte-exact round-trip.

*Passes:* `raw-minimal`, `escape-*`, `external-session-id`, `file-clock-metadata`,
`descriptive-metadata`, `custom-block`, `reject-*`, and the structural half of
the `discontinuity-*` pair.

### Phase 2 — the JSONL face

`discontinuity` projection, `input_extents` arrays, base64 for `bytes` options,
`extent`/`width` in the 64-bit set.

*Passes:* every `accept` vector's `.jsonl` compares equal in both directions.

### Phase 3 — positional arithmetic

D1. Widths become terms; `stream_extent` and `SessionReader.ranges` follow.

*Passes:* `discontinuity-known-width` — the vector exists because a reader
ignoring `width` computes `[50,80)` where a correct one computes `[75,105)`, so
this is the one number that proves the phase — plus `discontinuity-unknown-width`,
`passthrough-discontinuity`, and `chain/*` unregressed.

### Phase 4 — conformance rules

Raw-file prohibitions, extent checking (D3), fan-out keying, at-least-once locked
by test.

*Passes:* `isolate-discontinuity-in-raw`, `isolate-extent-exceeds-coverage`,
`isolate-extents-disagree`, `session-fan-out`, `isolate-coverage-gap`
unregressed.

### Phase 5 — the splice duty and the write side

`check_splice` (D2), `StreamView` surfacing breaks, `SessionWriter` and
`DecodeStage` emitters, pass-through renumbering, `rewrite_decoded` (D5).

*Passes:* the `splice/` fixture — which needs a **pairwise** harness case, since
both files are individually clean. This is also the first fixture upstream's own
`check.py` walks, so our harness's `files`-key handling needs the same
attention.

### Phase 6 — prose

`CLAUDE.md` ("the standard" becomes `0.14`, and the two traps still hold —
`0.9` stamps `1`/`0`, and `decoder_id` still does not decide a file's kind),
`README.md`, `docs/user/`, `docs/api/`, `DECODER_API.md`, `CONTENT_TYPE_API.md`.

Three prose items are not renumbering:

- **correspondence, not identity** — the decoder docs currently say a decoder
  frames. It frames *and may transform*.
- **the recoverability walk** reaches corresponding bytes, not the same bytes.
- **"Possible future extensions" is now "Design decisions not taken"**, and the
  four things we might have cited as planned (random-access index, per-session
  integrity counts, SCTP, decrypted tunnels) moved to the issue tracker. Any doc
  of ours pointing at that section needs a new target.

---

## What this does not change

Recorded so the phases are not padded with re-verification:

- **The frame, the TLV codec, and the merge.** No change at all.
- **Span-on-span overlap.** Already legal in our checker; now legal in the spec.
- **Coverage keyed per input stream.** Already right; `session-fan-out` proves it.
- **Any file we can currently write.** `0.13` broke nothing that was conformant
  under `0.12`, and `0.14`'s two tightenings both concern the new block. What
  changes is the stamped minor, which makes our old files unreadable for the
  ordinary `0.x` reason rather than because of anything in this release.

## Sizing

Phases 0–2 are a day of mechanical work with good test coverage. Phase 3 is
small but high-risk — it touches the one function every derived-file consumer
depends on. Phase 4 is the largest single chunk. Phase 5 is where the design
decisions land, and D5 is the only place we would be writing something the
standard does not ask for.

Total: comparable to the `0.12` port's Phases 3–6, without the 0.9 flag-day.

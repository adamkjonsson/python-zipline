# Migration plan: `python-zipline` 0.20 → 0.21

Against the [0.21 specification](https://github.com/adamkjonsson/zipline/blob/v0.21/docs/zipline-payload-format.md),
its [CHANGELOG](https://github.com/adamkjonsson/zipline/blob/v0.21/CHANGELOG.md),
[RELEASE-0.21-PLAN.md](https://github.com/adamkjonsson/zipline/blob/v0.21/docs/RELEASE-0.21-PLAN.md)
and the 62 vectors at tag `v0.21` (commit `4964dee`, cut 2026-09-18). We ship
`0.4.0` (2026-09-17) on `0.20`; this is a one-release jump.

> **Status, 2026-09-18: Phases 0–1 done.** Phase 0 is on `main` (PR #73):
> the tree is byte-identical to the tag; the projection sweep found no new
> defect (49 two-faced files, 0 disagreements on projected keys; the
> `adjacency` byte of all 61 Participant blocks checked against the `.jsonl`
> directly, 0 mismatches). Phase 1 is on branch `spec-0.21-port`: gate at
> `(0, 21)`, `0.5.0.dev0`, every literal moved; the golden test needed
> nothing, asserting through `SPEC_VERSION` since `0.20`. **Phase 2 done**
> on the same branch: `Adjacency`, `Participant.adjacency`, `<QHBB>`, the
> JSONL key after `pid`, both directions; the four spec examples quoted in
> `test_jsonl.py` and the `cli.md` sample gained the key as the spec's own
> text did. **Phase 3 done**: `serial_delta` in `zpf.order`, `_Placer`
> replacing `_offset_of` at all three sites, the checker anchored on the
> last placeable record (`anchor_seq`/`placed_any` replacing #70's
> `first_seq`), the two #70 tests inverted. One shape in the plan needed
> restating: with no `isn`, below-the-first-byte *is* below-the-predecessor,
> so through the reader the ordering rule isolates it first; the test drives
> `record_ranges` directly for the placer's own answer. **Phase 4 done**,
> with two departures from the text below: an unknown `adjacency` is ruled
> at the **Participant block**, not at close, because it is decidable
> there and a checked writer should refuse it at the point of writing;
> and `units` on a transport participant is reported at the **first
> record** that settles the layer, because advisory findings can only be
> raised from `observe` — which also makes the checked writer refuse it,
> so D4's writer-side MUST NOT is already met. Suite: 994 passed, 0
> failed, 7 xpassed — all seven new vectors, promoted in Phase 6.
> Every number below comes from the scratch run, not from the changelog.

---

## What we are actually migrating to

**`0.21` is a design release, and unlike `0.20` it changes what a reader
computes.** Upstream calls it "the first since `0.15` to change a block body".
Two rules, one body field, one reason word, two rulings. No byte of any
existing file changes, and no extent under 2 GiB moves — but the library reads
two of the new vectors wrong, drops a byte on re-encode of two more, and stays
silent on two negative ones. What moved:

1. **Offsets unwrap along stored order**
   ([#146](https://github.com/adamkjonsson/zipline/issues/146)). `0.20`
   measured every record against the origin under serial arithmetic, and said
   itself that the floor was undecidable past 2³¹ — which made every record
   more than 2 GiB into a stream *below the origin*. The rule now: record
   *k*'s offset is record *k−1*'s plus the **signed serial delta** (RFC 1982)
   of their `seq_start`s; the first record's is its delta from the origin
   (`isn + 1`, or the first captured byte); and an **unplaceable record
   anchors nothing** — the next record measures from the last *placeable*
   one. Well-defined at any length because the ordering rule keeps each
   record within 2³¹ of its predecessor. *Below the origin* and *out of
   order* are now stated as one case seen from two sides. Two vectors:
   `stream-past-2gib` and `stream-wraps-seq`.
2. **`adjacency`, a Participant Descriptor body field**
   ([#80](https://github.com/adamkjonsson/zipline/issues/80),
   [#106](https://github.com/adamkjonsson/zipline/issues/106)). The reserved
   `u16` after `participant_id` is now `adjacency: u8` + `_reserved: u8`.
   `0 = contiguous` — what every file ever written holds there and meant;
   `1 = units` — the participant is a **unit sequence**: its offset space is
   still the stored-order concatenation, every record addressable and
   citable, but **no two adjacent records may be assumed to join**. The
   wholesale form of a Discontinuity, for the two shapes with no honest
   per-seam form: a stage that reorders (#80) and a decoder whose units
   decompose one another (#106, nested DNS fields). A body field, not an
   option, for the reason `output_layer` is one: an ignored option would
   splice at every seam. Three keywords: a consumer **MUST NOT** treat any two
   records of a `units` participant as contiguous, and a decode stage reading
   one carries the break at every seam; a reordering stage **MAY** declare
   `units` instead of a block per seam; a writer **MUST NOT** set `units` on a
   transport-layer participant, where the reader ignores it, reports it and
   accepts — the transport-layer-label treatment. It is the **third
   load-bearing enum**: an unrecognised value is the isolate condition `kind`
   and `output_layer` already have, and a reader MUST NOT fall back to
   `contiguous`. The seam predicate does not apply to a `units` participant;
   the block stays permitted there. Four vectors: `unit-sequence-reversed`,
   `unit-sequence-nested`, `isolate-unknown-adjacency`,
   `advisory-transport-adjacency`. **Every participant `.jsonl` line gains
   `"adjacency"`**, which is why 55 projections change and no `.zpf` does.
3. **`capture-gap` as a Session End reason**
   ([#147](https://github.com/adamkjonsson/zipline/issues/147)). A hole of
   2³¹ bytes or more between two consecutive records is the one shape the walk
   cannot place; the producer ends the session at the hole and opens another
   on the same key with no `isn`. That exit was always conformant; the word is
   new, and a SHOULD. Open vocabulary, so a reader already reads it. Vector:
   `session-split-capture-gap`.
4. **Two rulings, no syntax.** Package D — delete `input_extents`,
   `reason_class`, the `dropped` MUST and the seam predicate — is **declined**
   after two releases of deferral (#125), with this library's having
   implemented every piece of it among the four reasons. The `u64 offset`
   Record option for the unmeasurable hole is deferred to a `1.x` minor, being
   safe to add later. Nothing to do here except stop expecting D: the
   `0.19`/`0.20` plans anticipated it, and the checker's apparatus stays.

**Vectors: 55 → 62**, 63 → 70 files, no removals. Every existing `.jsonl`
regenerated for the new key; `.zpf` bytes unchanged except the stamp.

---

## What the suite said before any code was written

Phase 0 was run in a scratch copy: vectors at `v0.21`, `SPEC_VERSION` patched
to `(0, 21)`, nothing else, every one of the 70 files read, projected and
measured against the manifest's `extents`. Result:

- **All 70 files read**; the five `reject` vectors are refused for the right
  reason (`reject-unknown-minor` now ships `22`).
- **55 of 55 projected files differ from their `.jsonl`** — at the participant
  line(s) only, for the missing `"adjacency"` key. The two `units` vectors
  project as `contiguous`, because the byte is parsed into `_reserved` and
  discarded.
- **`adjacency = units` is lost on re-encode.** `Participant._encode` packs
  the reserved half-word as `0`, so `dataclasses.replace(p).to_bytes()` on
  `unit-sequence-reversed`'s participant differs from the original at byte 10
  (`01` → `00`). A pass-through built on this library would silently turn
  a unit sequence into a stream that splices, which is the exact failure the
  field was put in the body to prevent. **This is the item with teeth** — and,
  Phase 1 found, the one the harness is *blind* to until Phase 2: the
  re-encode, JSONL → binary and own-writer tests all compare dataclasses, and
  a dataclass with no field for the byte compares equal on both sides. Only
  `test_accept`'s projection half sees it (`units` projects as `contiguous`).
  All three become sensitive the moment the field exists, which is the
  argument for adding it before anything else.
- **`stream-past-2gib` measures 1073741832 against a declared 3221225480** —
  the reading the vector's summary names as wrong. `_offset_of` in
  `reassembly.py` tests every record against the origin, so records 3 and 4
  are unplaceable. The checker's `_check_placement` emits the same wrong
  unplaceable notes (a channel, not a diagnostic, so the file still
  "accepts").
- **`stream-wraps-seq` measures 16, correctly** — the mod-2³² subtraction
  already handles a wrap under 2 GiB. **`unplaceable-below-origin` measures
  16, correctly** — with `isn` present the origin is fixed and the
  unplaceable record never anchored anything here either. Both stay right
  under the walk; neither is a reason to skip it.
- **`isolate-unknown-adjacency`: 0 diagnostics, manifest wants 1.** No check
  exists.
- **`advisory-transport-adjacency`: 0 diagnostics, manifest wants 1.** No
  finding exists.
- **`session-split-capture-gap`, `unit-sequence-reversed`,
  `unit-sequence-nested`: accept cleanly, extents 8/8, 160, 17 — all
  correct.** The two `units` vectors pass the seam predicate only because
  every pair in them is `A ≥ B`, which the predicate already declines. A
  `units` participant with a hole-class Undecoded between two *ascending*
  records would fire falsely today; no vector ships that shape, but the
  exclusion is a stated clause of the predicate and goes in regardless.
- All 41 other declared `extents` match.

**One thing the scratch run predates.** `0.4.0` landed
[#70](https://github.com/adamkjonsson/python-zipline/issues/70) after the run:
the checker's `_check_placement` no longer returns early without an `isn`,
and instead applies the origin floor from the first captured byte
(`_ParticipantState.first_seq`). That closed the reader/checker disagreement
#70 reported — but it closed it on the **`0.20` reading**, the one where an
`isn`-less stream past 2 GiB reads its later records as below its own first.
Its changelog entry says so in as many words ("that ceiling is the format's,
zipline#146, still open; until it moves…"), and two tests pin the reading:
`test_without_an_isn_the_reader_reports_every_record_it_zeroes` in
`test_reassembly.py` and
`test_without_an_isn_the_first_captured_byte_is_the_origin_and_has_a_floor`
in `test_conformance.py`, both asserting that records 2 GiB and 3 GiB into a
stream with no handshake are unplaceable. Under `0.21` they are placeable and
the extent is `3 GiB + 4`. **Those two tests invert in Phase 3** — they are
not fixtures, they are our own assertions of a rule the specification has
since replaced — and become the `isn`-less half of the past-2-GiB positive
test. `test_with_an_isn_the_origin_is_isn_plus_one_not_the_first_record`
stays as it is; it is right under both readings.

So the port is: one body field through every layer (blocks, JSONL, writer,
reader, transform, decode), one arithmetic rule in two modules, two checker
conditions, the harness, and prose. **Two rules are implemented in this port**,
which `0.20` did not need and `0.19` did.

---

## Decisions

### D1 — `Adjacency` is an `IntEnum` shaped exactly like `OutputLayer`

`Adjacency.CONTIGUOUS = 0`, `Adjacency.UNITS = 1`, exported from `zpf`.
`Participant.adjacency: Adjacency | int = Adjacency.CONTIGUOUS`, kept as a
raw `int` when the byte is a value this version does not define — the
`OutputLayer` convention, because it is the same kind of enum: load-bearing,
so it is never guessed, never defaulted, preserved through a round-trip, and
isolated by the checker. `_PARTICIPANT_BODY` becomes `"<QHBB"`; the trailing
`u8` is written `0` and ignored on read. The default is `CONTIGUOUS` because
that is what silence has always said, and the specification numbers it `0`
for exactly that reason.

### D2 — one walker, used everywhere the offset space is computed

`_offset_of(record, origin)` was made the single definition of placement to
close [#63](https://github.com/adamkjonsson/python-zipline/issues/63); it
stays single, but it is no longer a function of the origin alone. It becomes
a small stateful placer — `_Placer(origin)` with `place(record) -> int |
None` — that carries the last *placeable* record's `(seq_start, offset)` and
returns `prev_offset + serial_delta(seq_start, prev_seq)`. The first record
measures from the origin, which is what "the origin is the first record's
predecessor" says. `None` is returned for no `seq_start` on an anchored
stream, and for a negative delta; a `None` does not advance the anchor.

`record_ranges`, `StreamView.chunks()` and `StreamView.units()` all take it
in place of the origin, so the three cannot drift. `stream_extent` follows
`record_ranges`. `ConformanceChecker._check_placement` uses the same
predecessor test, so the reader and the checker agree on which records are
unplaceable — which is the property the extent test in the harness is
built to hold.

Below-predecessor for a non-first record is the ordering violation
`_check_record_order` already raises, and the specification now says so in
one sentence. So the checker's unplaceable *note* only ever describes the
first placeable-candidate record against the origin, or a hint-less record on
an anchored stream; the placer still handles a negative delta on its own,
because `StreamView` can be driven with the checker off.

`serial_delta(a, b)` — the signed RFC 1982 difference — is added to
`zpf.order` beside `seq_lt`/`seq_leq`, since that module is where the
arithmetic lives and the rule names it.

### D3 — the consumer face of `units` is `StreamView.units()`, and only that

Where a decoded participant declares `units`, `StreamView.units()` yields a
:class:`Break` **before every record after the first**, with `width=None`,
`reason=None`, and a new `declared: bool` attribute set `False`. A declared
Discontinuity in such a participant is still yielded where it sits, with
`declared=True`, and its `width` still counts in the arithmetic.

This goes one step beyond the standard and is recorded as such: the
specification says a consumer MUST NOT treat any two records of a `units`
participant as contiguous, and leaves *how* a reader surfaces that to the
reader. Synthesising a break per seam is the reading that lets the existing
`for unit in view.units(): if isinstance(unit, Break): flush()` idiom stay
correct without a second branch — a consumer written against `0.20` that
honoured breaks honours unit sequences unchanged. The `declared` flag is
what stops the synthetic ones being mistaken for a producer's statement.

`datagrams()` is records-only and unchanged, as its docstring already warns.
`chunks()`, `segments()` and `reassembled()` require a stream-oriented
participant, and a stream-oriented one is transport-layer, where the field
is ignored — so nothing there changes. `StreamView.is_unit_sequence` is added
as a property, resolved from the participant and the layer: `True` only for a
decoded participant declaring `units`, so a transport participant carrying
the field answers `False`, which is the specification's "ignores the field".

### D4 — the producer face is a keyword, not an enforcer

`SessionWriter.participant(..., adjacency=Adjacency.CONTIGUOUS)`. Every place
the library re-declares an input participant in an output — `Derived.handle`
in `writer.py`, `_copy_participant` in `transform.py`, the decode stage's
scaffolding — **carries the field forward verbatim**, because a pass-through
that dropped it would splice.

The new decode-stage duty — a unit whose spans cross two input units of a
`units` input either sits in an output participant declared `units` or has a
Discontinuity where they meet — is *not* enforced. The library does not
enforce the existing twin either (a unit whose spans cross a declared
Discontinuity), and for the same reason: which input units a citation
crosses is the stage's knowledge, and `units()` (D3) is how the stage is
told. `DecodeStage` gains an `adjacency=` on its participant re-declaration
so a reordering or decomposing stage can say `units` for its output; the
`Seam` mechanism stays for the per-seam form, and `reordered-decoded` keeps
proving it.

The writer MUST NOT set `units` on a transport-layer participant. The layer
of a participant is not known when the Participant block is written — it
resolves from its records' decoders — so the checked writer rules on it at
session end, as a `SemanticError` of the same family as the other
close-time checks. An unchecked writer writes what it is told.

### D5 — `capture-gap` is a docstring and a documented constant, nothing more

`SessionEnd.reason` is an open vocabulary and the library holds no producer
that decides when to split a session. The word lands in the `SessionEnd`
docstring and `docs/user/concepts.md` beside `capture-end`, with the
distinction the specification draws (the capture stopped, or it resumed and
the stream could not). No API.

### D6 — library version becomes `0.5.0.dev0`

A spec port is a library minor bump. `0.4.0` shipped on 2026-09-17 with an
empty `[Unreleased]` above it, so this port's entry is the whole of
`[Unreleased]` and the version goes `0.4.0` → `0.5.0.dev0`. The entry should
close the loop #70's entry left open — "until the format moves" — since this
is the release in which it moved.

---

## Phases

Each phase ends green under `.venv/bin/pytest` and `ruff check`. Phases 2–5
are ordered by vector: each ends with named vectors leaving `xfail`.

### Phase 0 — re-vendor the suite

Copy `vectors/` at `v0.21` verbatim (excluding `build.py`/`check.py`), verify
with `diff -r`, update `tests/vectors/VENDORED.md` (tag, 62/70, the seven new
names, `advisory` count now three). Run the projection sweep: expect every
participant line to differ until Phase 2 and nothing else to. The suite is
*red* at the end of this phase — every `.zpf` is refused at the gate.

### Phase 1 — move the gate, and every literal

`SPEC_VERSION = (0, 21)`. `Specification` URL in `pyproject.toml`. The golden
assertion in `tests/test_golden.py`. `zipline-payload/0.20` in
`tests/test_jsonl.py` and `docs/user/cli.md`. Version to `0.5.0.dev0` (D6).
`test_every_case_has_a_file`'s count to 70.

After this phase: every vector reads; `KNOWN_PASSING` names other than the
two `units` vectors pass `test_accept`'s diagnostics half; **every projected
vector fails its projection** on the participant line. That is the expected
red, and Phase 2 clears it wholesale.

### Phase 2 — `adjacency` through blocks and JSONL

`Adjacency` enum (D1); `Participant.adjacency`; `_PARTICIPANT_BODY = "<QHBB"`;
encode and parse. JSONL: `_ADJACENCY_LABELS` beside `_LAYER_LABELS`, always
emitted after `pid` (the shipped `.jsonl` puts it there), parsed as a label or
a raw number exactly as `output_layer` is. Unit tests in `test_blocks.py` and
`test_jsonl.py` for the round trip of `0`, `1` and `2`.

Leaves xfail: 55 projections, the two `units` re-encodes. Still xfail after
this phase: `stream-past-2gib` (extent), `isolate-unknown-adjacency`,
`advisory-transport-adjacency`.

### Phase 3 — the unwrapping walk

`zpf.order.serial_delta`. `_Placer` in `reassembly.py` (D2) replacing
`_offset_of` in `record_ranges`, `chunks()`, `units()`. Rewrite the
`_offset_of` docstring's "undecidable beyond 2³¹" paragraph — it is now
wrong, and it is the paragraph the specification retired. The checker's
`_check_placement` tracks the last placeable `(seq_start, offset)` per
stream and tests against it, with the origin as the first record's
predecessor. #70 already made the first captured byte a floor when there is
no `isn`; what changes is *what* is measured against it — the first record
only, every later one against its placeable predecessor.
`_ParticipantState.first_seq` (#70) is subsumed by the placer's anchor and
goes; `last_seq` is the ordering rule's anchor and keeps its meaning. The
#70 paragraph in `_check_placement`'s docstring — "the only way below the
origin is around it… that is the format's ceiling" — describes the reading
this phase removes and is rewritten, not kept as history.

Unit tests: the four shapes in `test_reassembly.py` — past 2 GiB with and
without an `isn`, wrap through 2³², unplaceable-anchors-nothing (extent 16
not 17), a hint-less stream with a below-first-byte record. The two #70
tests named above are inverted rather than deleted: same four records 1 GiB
apart, no `isn`, now asserting four placeable ranges, extent `3 GiB + 4`,
and **no** unplaceable note. Leaves xfail: `stream-past-2gib`.

### Phase 4 — the checker

`_ParticipantState.adjacency`. At participant close: an `adjacency` that is
not an `Adjacency` → `SemanticError` "which this version does not define",
the `output_layer` sentence with the noun changed; `Adjacency.UNITS` on a
stream resolving to `TRANSPORT` → advisory note in the shape of
`_transport_label`. `_track_break_candidates` and `_check_participant`
exclude a `units` participant from the seam predicate — at close, where the
layer test already lives, so the predicate's first clause reads "decoded
layer, not `units`" in one place.

`_ISOLATE_REASONS["isolate-unknown-adjacency"] = "does not define"`. Leaves
xfail: `isolate-unknown-adjacency`, `advisory-transport-adjacency`. A
checker test for the false-fire shape no vector ships: `units` participant,
ascending spans, hole-class Undecoded between — must *not* raise.

### Phase 5 — writer, reader, transform, decode

`SessionWriter.participant(adjacency=)`; the checked writer's close-time
MUST NOT (D4). `Derived.handle`, `_copy_participant`, and the decode stage's
participant scaffolding carry the field. `StreamView.is_unit_sequence` and
the synthetic `Break(declared=False)` in `units()` (D3); `Break.declared`
defaults `True` so existing constructions are unchanged. `DecodeStage`
participant re-declaration accepts `adjacency=`. `test_a_vector_survives_our_own_writer`
already covers the two `units` vectors once the writer takes the keyword.

Unit tests: a pass-through of `unit-sequence-reversed` through `merge_files`
/ a decode stage preserves `units`; `units()` on it yields three undeclared
breaks; the checked writer refuses `units` on a participant whose records
resolve to transport.

### Phase 6 — the harness

`KNOWN_PASSING` +7 → 62 names, 70 files. Module and data docstrings retold
for `0.21` (the "55 names, 63 files" arithmetic, `_NEW_OPTION_IDS`'s note
that `output_layer` was a body field — now `adjacency` is the second one,
and the same reasoning applies: it has no option id and no escape to hide in,
so a dropped `adjacency` shows up in the re-encode and projection tests, which
Phase 0 confirmed). `DEFECTIVE` and `UNIMPLEMENTED` stay empty. Zero xfail,
zero xpass.

### Phase 7 — the changelog

An `[Unreleased]` entry naming `0.21`, stating that `0.4.0` files are
refused at the gate, and listing what a consumer sees: streams past
2 GiB measure correctly; `Participant.adjacency` and `Adjacency`;
`units()` surfaces unit sequences as breaks; two new checker conditions;
`capture-gap`. Under **Changed**, that `Break` gains `declared`. Under
**Decided**, mirroring upstream's new category: Package D declined, and what
that means for `check_extents`/`CoverageLedger` — they stay.

### Phase 8 — the documentation sweep

**Last, and a sweep, not a list.** Phases 2–5 each touch the docstrings of
what they change; this phase is the audit that the prose surface as a whole
describes the library as it now is. It is last because every earlier phase
can move a sentence somewhere this one has to find, and the `0.16` port's
lesson (`docs/dev/contributing.md` § Porting, step 5) was that a sweep done
from a list misses what the list did not name.

The method, in order:

1. **Grep for the old version.** `0.20`, `v0.20`, `0\.20` across `README.md`,
   `CLAUDE.md`, `docs/`, `src/`, `tests/`, `pyproject.toml`,
   `VECTOR-DEFECTS.md`, `tests/vectors/VENDORED.md`. At the time of writing
   that is 15 files; expect the number to have changed by the time this
   phase runs. Every hit is either a bare number to bump or an argument to
   rewrite — decide which, per hit, rather than replacing.
2. **Grep for the retired readings.** The phrases `0.21` removed from the
   specification are the ones most likely to survive in our prose, because
   they were *correct* when written: "below the origin" stated as the only
   floor; "undecidable beyond 2³¹" / "the format's ceiling" / "the only way
   below the origin is around it" (#70's wording, now in `docs/user/errors.md`
   and `docs/user/concepts.md` as well as the code); "two load-bearing enums";
   "a Discontinuity at each seam" stated as the only form for a reordering
   stage (`docs/user/guides/decoding.md` § the stage's duty,
   `docs/dev/conformance.md` § the origination duty); `capture-end` listed
   without `capture-gap`. Each is a sentence to rewrite, not a number.
3. **Read every page, in this order, against the code as it now stands** —
   not only the pages the greps hit:
   - `README.md`, `CLAUDE.md` — the "two traps" paragraph gains the
     unwrapping rule and `adjacency`; vector counts (62 names, 70 files, three
     advisory); the port is no longer "complete" at `0.20`.
   - `docs/index.md`.
   - `docs/user/concepts.md` — *unit sequence* as a term, beside stream;
     the offset walk in the offset-space section; `capture-gap` beside
     `capture-end`; the third load-bearing enum.
   - `docs/user/guides/reading.md`, `decoding.md`, `provenance.md`,
     `ordering.md`, `faces-and-io.md` — `is_unit_sequence`, the synthetic
     `Break(declared=False)` in `units()` with the **beyond-the-standard
     callout** D3 requires (the specification says MUST NOT splice, not how a
     reader surfaces it); the reordering stage's two honest forms; the
     JSONL participant line's new key.
   - `docs/user/howto/decode_stage.md`, `merge.md`, `validate.md`,
     `robustness.md`, `convert.md`, `payload_content.md` — `adjacency=` on
     the decode stage's participant; the two new checker conditions in
     `validate.md`'s table; `errors.md`'s diagnostic catalogue gains both,
     and its below-origin paragraph is rewritten for the predecessor floor.
   - `docs/user/tutorial.md`, `tutorial-decoding.md`, and the seven
     `docs/user/examples/*.py` — run by `test_tutorial_examples.py`, so they
     pass or fail rather than drift; still read them, because a passing
     example can teach the per-seam form where `units` is now the truer one.
   - `docs/user/cli.md` — the `zipline-payload/0.21` sample; whether `zpf
     info` should print `adjacency` beside the endpoint (it prints
     nothing per participant today but the endpoint; a `units` participant
     is worth a word).
   - `docs/api/*.md` — autodoc, so the content is the docstrings; check
     that `Adjacency`, `Break.declared`, `StreamView.is_unit_sequence` and
     `serial_delta` are *listed* where their neighbours are, since autodoc
     only renders what a page names.
   - `docs/dev/architecture.md` (`_offset_of` → the placer, if named),
     `conformance.md` (the rule table gains the three new rules and the
     seam predicate's first clause; "two load-bearing" → three),
     `testing.md` (the three load-bearing tests, if any is retold),
     `contributing.md` (the porting checklist: add "grep for the retired
     readings" as a step, since this port is the first where the
     specification retired sentences we had quoted), `option-exposure.md`
     (`adjacency` is a body field, not an option — say so where
     `output_layer` is said to be).
   - `VECTOR-DEFECTS.md` (tag reference; no new defect at `v0.21`),
     `tests/vectors/VENDORED.md` (done in Phase 0; re-read).
   - `plans/README.md` — this plan's row moves from "planned" to done, and
     the `0.20` row loses "the version the library implements today".
4. **Docstrings are documentation.** `grep -n "0.20\|2³¹\|below the origin"
   src/zpf/*.py` after step 3, since the API pages render them. The
   `_offset_of` paragraph Phase 3 rewrites is the largest; `OutputLayer`'s
   docstring says it is load-bearing "like `SourceKind`" and should now name
   `Adjacency` too; `Participant`'s attribute list; `SessionEnd.reason`'s
   vocabulary (D5); `RecordFlags` is untouched.
5. **Build and diff.** `.venv/bin/sphinx-build -W docs docs/_build/html`
   clean; then a grep of the built HTML for `0.20` as the last check that
   nothing the sources include by reference (the examples, autodoc) still
   says it.

**Done when:** the three greps (steps 1, 2, 4) return only the changelog's
history and this plan's own text; every page in the list has been read; the
`-W` build is clean; and `test_tutorial_examples.py` passes. Note in this
plan's status line which pages needed a rewrite rather than a renumber, for
the next port's list.

---

## What this does not change

- No block, option or enum is removed. No existing field moves.
- `OutputLayer`, `SourceKind`, `stream_layer`, `stream_kind` — the two-axis
  model is unchanged; `adjacency` is a third property of a participant, not
  a third axis of a stream's offset space.
- The seam predicate's two arms, `reason_class`, `input_extents`, the
  `dropped` MUST — Package D is declined and all four stay.
- `Seam` and the per-seam `Discontinuity` form; `reordered-decoded` keeps
  its block and keeps passing.
- `merge_files` and the coverage ledger: a `units` participant's coverage is
  the same union of spans as any other.

## Open questions

- **`Break.declared` versus a distinct class.** A `UnitSeam` type would keep
  `Break` meaning exactly "a Discontinuity block", at the cost of a second
  `isinstance` in every consumer. D3 picks the flag; revisit if the first
  decoder written against it disagrees.
- **Whether the checker should note, not raise, a below-predecessor record
  when the ordering check is disabled.** Today `_check_record_order` always
  runs; if a lenient mode is ever added, the placer's `None` is the fallback
  and the note wording in `_check_placement` should already cover it.

## Sizing

Two to three days. Phases 0, 1, 6 and 7 are mechanical. Phase 2 is small
but touches every layer. Phase 3 is the one with arithmetic in it and needs
its unit tests written before the code. Phase 4 is two conditions in a
checker whose shape already has both. Phase 5 is the API surface and the
only phase with design judgement left in it (D3, D4). Phase 8 is the widest:
some forty pages and docstrings, most of which need one number changed, and
perhaps eight of which — the ones that argued from the origin floor, the
per-seam form, or the count of load-bearing enums — need a paragraph
rewritten. Budget half a day for it and do not fold it into the earlier
phases; the point of doing it last is that it checks them.

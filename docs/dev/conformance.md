# Conformance strategy

The specification defines obligations at three depths, and `zpf` enforces
each at the point where it is cheapest and hardest to get wrong. The rule of
thumb: **structural** faults are caught while framing bytes, **semantic**
faults while observing blocks in a single pass, and **whole-file** properties
by the transform that owns them.

## Where each class of rule is enforced

| Rule class | Examples | Enforced by | Tier |
| ---------- | -------- | ----------- | ---- |
| Structural framing | magic, version, `tick_hz != 0`, block length a multiple of 4, lengths within bounds | `binary.py` (`BlockReader`) while decoding | `StructuralError` — always fatal |
| Value encodability | integer range, option ≤ 65 535 bytes, `Custom` length a multiple of 4 | `blocks.py` / `_frame.py` at construction/serialization | `EncodeError` — write side |
| Semantic (single-pass) | declare-before-use, id uniqueness, session lifetime, per-participant `seq_start` order, file-kind purity | `conformance.py` (`ConformanceChecker`) | `SemanticError` — isolate or reject |
| Semantic, writer-only | reserved flag bits, `prim:` payload widths and token vocabulary | `conformance.py` (`ConformanceChecker`) | `AdvisoryError` — report, but keep the block |
| Sequenced order | a SEQUENCED session's stored order really is a causal linearization | `order.py` `_StoredOrder`, driven by `SessionWriter` while writing and by `verify_sequenced` / `SessionReader.verify()` on request | `SemanticError` — write side always, read side opt-in |
| Coverage, from the file alone | an interior range neither decoded nor marked; a declared `input_extents` its own spans contradict | `conformance.py` (`CoverageLedger`), ruled on at `finish()` | end-of-stream property |
| Coverage, against the input | every input offset decoded or marked Undecoded, never both; a declared extent the input disagrees with | `transform.py` `check_coverage` | needs a second file |
| The splice duty | a unit whose spans cross an input's declared break | `transform.py` `check_splice` | needs a second file |

**Coverage is settled in two places, and the split is the point.** Some of it a
file can be checked for alone: a hole *between* two covered ranges is visible
without opening anything, and since `0.14` a Session End's `input_extents`
makes a *trailing* hole visible too — without a declared length, coverage that
stops early is indistinguishable from a stream that was that short. That much
the `ConformanceChecker` gathers as it observes and rules on at `finish()`, so
`zpf.open` reports it. The rest genuinely needs the input in hand, and stays in
the transform helpers.

**Sequenced order is enforced on the write side and offered on the read side**,
and the asymmetry is deliberate. The rule is two clauses — each participant's
`seq_start` non-decreasing, and (pairwise) no record after a peer record that
already acknowledged its bytes — so it is single-pass and O(1) per participant,
not the k-way merge. `order._StoredOrder` implements it once and both sides
drive it.

The writer runs it unconditionally for a `sequenced=True` session, because that
flag is a promise the producer is *making*, and a promise nothing checks is one
readers will trust and be misled by: a reader of a sequenced session skips the
merge, so a bad interleaving is not merely unnoticed, it is acted on. Only the
per-participant clause was ever enforced there — it lives in the
`ConformanceChecker` and both participants can be individually in order while
the interleaving is wrong, which is exactly the case that used to escape.

The `ConformanceChecker` does not host the second clause, even though it could
afford to. It is shared with the read path, where the format lets a reader
**trust** a sequenced session's stored order rather than re-derive it; putting
the rule there would make every `zpf.open` re-litigate a promise the file
already makes. So on the read side it stays opt-in, as
{meth}`~zpf.reader.SessionReader.verify` and `zpf validate --verify`.

One guard is load-bearing enough to name here: the interior-hole check runs for
an input stream **some record's `spans` cited**. Through `0.18` that was a way
of saying "a decode stage only", because a pass-through cited nothing; since
`0.19` it cites everything and is answerable for its inputs exactly as a decode
stage is, which is why {func}`zpf.merge_files` marks its inputs' holes. What
the gate still excludes is the shape the specification names separately: a
`zpf-input` Source declared only so that an *inherited* Undecoded block still
resolves. No record's spans name it, so this file is not answerable for it.

## The ConformanceChecker

`conformance.ConformanceChecker` is the heart of the semantic tier: a
single-pass observer fed blocks in file order via `observe()`, raising
`SemanticError` on the first violation. It is wired in three ways:

- **The ergonomic writer uses it always.** Every block `create()` produces is
  observed before it is written, so `zpf.create` *cannot* emit a
  nonconformant file — this is the "conformant by construction" guarantee.
- **The flat writers use it on request** — `BlockWriter`/`JsonlWriter`
  constructed with `check=True`.
- **Standalone** — `ConformanceChecker().check(blocks)` over any block
  iterable, e.g. to validate a stream a lower-layer tool produced.

Three design points worth preserving when editing it:

- **Bounded memory.** Per-session state is freed at each Session End; only the
  set of ended session ids is retained (to police the nothing-after-Session-
  End rule). The checker runs on unbounded streams.
- **Consistent-on-raise.** Each handler runs its pure checks *before* mutating
  state, so a raised violation leaves the checker
  consistent and a lenient reader can isolate the offending block and carry
  on.
- **Isolating versus advisory findings.** A few MUSTs bind the writer only,
  because they leave a reader nothing to act on: it is told to ignore the
  offending label or bits and use the block, whose bytes are the source of
  truth either way. Those call `_note()` instead of raising; `observe()`
  raises the collected findings as one `AdvisoryError` (a `SemanticError`
  subclass) *after* the handler returns, so a checking writer still refuses
  the block while a lenient reader reports a `nonconformant` diagnostic and
  hands the block over. Reporting after the handler is the mirror image of
  consistent-on-raise: a kept block must be fully counted first. An
  isolating violation found in the same block wins — it raises from inside
  the handler, and the notes go with the dropped block.

  The advisory rules today:

  | Rule | Why a reader can only ignore it |
  | ---- | ------------------------------- |
  | An illegal `prim:` token, or a width that disagrees with `payload_len` | The spec says to treat the label as unknown and keep the payload: "MUST NOT pad, truncate, or reinterpret". |
  | A `content_type` at the **transport** layer | Dropping the label loses nothing and the record stays fully readable, so there is no unit a reader could soundly discard. `role` joins this row when Phase 6 implements it — the bar names both labels in one sentence and gives them one strength. |

  **Reserved flag bits are not an advisory rule**, though they read like one.
  The specification groups a nonzero reserved field with unknown block types
  and unknown option ids as part of the extension mechanism — "not a violation
  … the normal, conformant path" — so the checker accepts them in silence and
  the bit survives uninterpreted. Diagnosing one would report conformant data
  as suspect. This table listed it until the `0.19` port, which is worth
  recording because the code comment saying so has been there all along.

### The unit is the stream, not the file

**There is no file kind, and inferring one was a bug.** Through `0.14` the
checker locked a file to exactly one of raw, decode-stage or pass-through at
the first distinguishing block — which rejected `mixed-derivation`, a
conformant file that decodes one session and passes another through. `0.16`
made provenance and layer independent per-stream axes, and the checker rules
per participant instead.

Two rules bind per participant and settle at Session End, because both are
properties of its *records* and declare-on-first-use puts the Participant
block first: its records must resolve to **one layer**, and a layer this
version does not define must not be guessed past.

**Provenance is a per-record rule, and there is one of it:** every
`zpf`-sourced record carries `spans`. Through `0.18` a derived stream was
*created* (records with `spans`) or *preserved* (a participant with `origin`),
policed by four rules; `0.19` removed the option, a pass-through writes an
identity span instead, and the four collapsed into that sentence. It binds at
the record rather than at Session End, which is earlier and simpler — the
block a lenient reader isolates is the one that broke it.

Any file holding a `zpf`-sourced stream still requires
`produced_by`/`produced_at` on the File Header.

## Reader side: structural versus semantic

A reader rejects a file only when the byte stream can't be trusted
(`StructuralError`, always). A well-framed block that breaks a semantic rule
is isolated — recorded as a `nonconformant` diagnostic in lenient mode, or
raised under `strict=True` — unless the finding is advisory, in which case the
diagnostic is recorded and the block still reaches the caller. Truncation is a
third, expected condition. This is the [errors page](../user/errors.md)'s
subject, from the reader's side.

## Going beyond the standard

Per `CLAUDE.md`, support must stay complete *and* must not silently exceed the
v0.19 spec: any behavior beyond the standard has to be flagged to the user with
an explicit callout. The checker's rules are the spec's, and the two out-of-band
checks (sequenced order, coverage) are spec requirements enforced elsewhere,
not extensions. A new rule that isn't in v0.19 does not belong in the
`ConformanceChecker`.

**One thing does exceed the standard, and it is on the write side only.**
{meth}`~zpf.SessionWriter.record` refuses a payload-carrying record whose
`seq_start` is below the stream origin. `0.19` permits that file — the record
is unplaceable, its bytes are in no offset, and a reader accepts and reports —
so this is a producer-side rule the format does not state. It is deliberate:
such a writer is discarding its own bytes, the cost is silent, and the only
instance anyone has met was the bug behind
[#63](https://github.com/adamkjonsson/python-zipline/issues/63). The refusal
names itself as stricter than the format in the error text, the docstring and
the [errors page](../user/errors.md#writing-one-refused-and-that-is-stricter-than-the-format),
which is what the callout rule asks for.

The reading side is not affected, and deliberately so: `zpf.open` accepts such
a file, places the record at zero width, and reports it under `unplaceable`.
Being stricter than the format about what we *write* costs a producer nothing
it wants; being stricter about what we *read* would refuse files the format
says are fine.

### What the standard asks for and no reader can check

Two rules are **writer-only**, and their absence from the checker is a
decision rather than an oversight.

**A stage emitting a transport layer MUST NOT withhold content from a stream
whose offsets are not sequence-anchored.** In a message-oriented or `N = 1`
stream with no `isn` — `tunnel/outer.zpf` is one — offsets are the
accumulation of what arrived, so a withheld datagram leaves no trace, and the
Discontinuity that would say so is barred by the layer. A file that withheld
and one that did not are byte-identical, which is precisely the defect. The
specification says outright that no reader can check it, and it ships no
vector for the same reason. A stage that needs to withhold from such a stream
emits a decoded layer instead, where the break is expressible.

**Most of the origination duty.** The rule is *do these two adjacent units
join?*, and it rests on producer knowledge: only the stage knows what it did
with its input. One case is decidable from a single file — a `hole`-class
Undecoded region between two adjacent units' input regions — and that one is
implemented, as the predicate in `_check_unmarked_breaks`. Satisfying it is
**not** satisfying the duty; it is the minimum a checker owes, deliberately
conservative, and every pair it declines to test may still be one where the
duty binds. On the write side {meth}`~zpf.DecodeStage.record` asks the
producer directly, through `seam=`.

### One recommendation the standard declined, then took

Our [review of `0.15`](https://github.com/adamkjonsson/python-zipline/blob/main/plans/SPEC-0.15-REVIEW.md)
argued (Finding 5) for splitting the bytes-exist vocabulary in two — `skipped`
for content withheld where the survivors still join, and something else for
content removed where they do not — so that a filter's duty would be decidable
from one file. `0.16` declined it, on the grounds that the reason word must not
decide the duty.

**`0.17` took it after all**, coining `dropped` for content that was removed.
The problem `0.16`'s reasoning left standing is what changed its mind:
`undecoded-skipped` and `filtered-decoded` were byte-shaped alike — a
bytes-class region between two adjacent units — and one owes a Discontinuity
while the other does not, so a checker raising on either raised wrongly on the
other. `dropped` is what tells them apart, and it is the second arm of
`_check_unmarked_breaks`.

The word still does not *decide* the duty; the test remains whether the
survivors join. What it does is let a producer state that it removed content,
which a checker can then act on. `0.18` closed the escape that left by making
`dropped` the **only** spelling for removed content, so a producer taking the
vocabulary's openness up on `{"reason": "filtered", "reason_class": "bytes"}`
can no longer say something true and sidestep the only single-file test on the
bytes side.

## Conformance vectors

The specification ships 53 hand-built vectors, vendored verbatim into
[`tests/vectors/`](https://github.com/adamkjonsson/python-zipline/tree/main/tests/vectors)
and run by `tests/test_vectors.py` across the three tiers — `accept` (a
conformant file, with its expected JSONL projection), `reject` (structural
corruption), and `isolate` (a semantic violation the reader must not pass
silently).

`0.16` added a key rather than a fourth tier: an `accept` entry marked
`advisory` declares **one** violation instead of none, and the reader must
both accept the file completely *and* report it. It is a key because a tier
names what a reader *does*, and a reader accepts these files. It is the
format's first violation that accepts, and the harness asserts both halves —
silence fails the case as loudly as rejecting it would.

Two habits keep them honest. The harness asserts what each negative vector is
refused or diagnosed *for*, not merely that something was raised — which matters
most at the start of a port, when the version gate is still behind the vectors
and fires before the check each one is testing. Three `reject` vectors passed for
that wrong reason during the 0.12 port, and two did again at the start of the
0.14 and 0.16 ones — held out of the harness's known-passing set until the gate
moved. And
a vector is never edited to make a test pass: they are subordinate to the
normative text, so a vector that looks wrong is a question for the spec
repository. Four have been found so far; `VECTOR-DEFECTS.md` records them, and
two are open against `v0.19`.

One vector is judged as a **pair**. `splice` ships two files that are each
individually conformant — stage 1 declares a break, stage 2 spans across it —
so the violation belongs to neither on its own and a harness testing files
individually passes it. The manifest's `files` key marks such a fixture, and
the harness routes it to {func}`zpf.check_splice` instead of the per-file
tiers.

## Where to go next

- [Architecture](architecture.md) — where these checkers sit among the layers.
- [Errors and diagnostics](../user/errors.md) — the exception tiers from the
  consumer's side.
- [Testing](testing.md) — `test_conformance.py` and the golden/merge tests
  that lock this behavior down.

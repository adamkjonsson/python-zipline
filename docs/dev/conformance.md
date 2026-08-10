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
| Sequenced order | a SEQUENCED session's stored order really is a causal linearization | `order.py` `verify_sequenced` / `SessionReader.verify()` | needs the merge algorithm |
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

Sequenced order remains **out of the single-pass checker's reach** entirely: it
needs the k-way merge to decide. The `ConformanceChecker` docstring calls that
boundary out explicitly.

One guard is load-bearing enough to name here: the interior-hole check runs for
a **decode stage** only. A pass-through re-emits records rather than citing
them, so it carries no `spans` and its only covered ranges are the Undecoded
blocks it inherited — everything before them would read as an unaccounted hole.
The coverage guarantee is a decode stage's obligation, and applying it to a
pass-through fails conformant files.

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
  | Reserved bits set in any flags field (File Header, Session, Record) | The format defines no meaning for them, so there is nothing to act on. Isolating would discard well-framed data — and dropping a File Header or Session Descriptor takes every block that depends on it. |
  | An illegal `prim:` token, or a width that disagrees with `payload_len` | The spec says to treat the label as unknown and keep the payload: "MUST NOT pad, truncate, or reinterpret". |

### File-kind purity

A file is exactly one kind — raw, decode-stage, or pass-through — and the
checker infers it from the first distinguishing block, then locks it: a
capture-sourced byte run means raw, a `decoder_id` means decode-stage, a
`zpf-input` byte run or a participant `origin` means pass-through. A later
block implying a different kind is the error, and the message names both the
block that locked the kind and the one that conflicts. Derived kinds
(decode-stage, pass-through) additionally require `produced_by`/`produced_at`
on the File Header.

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
v0.16 spec: any behavior beyond the standard has to be flagged to the user with
an explicit callout. As of this version nothing does — the checker's rules are
the spec's, and the two out-of-band checks (sequenced order, coverage) are
spec requirements enforced elsewhere, not extensions. A new rule that isn't in
v0.16 does not belong in the `ConformanceChecker`.

### What the standard asks for and no reader can check

Two `0.16` rules are **writer-only**, and their absence from the checker is a
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

### One recommendation the standard did not take

Our [review of `0.15`](https://github.com/adamkjonsson/python-zipline/blob/main/SPEC-0.15-REVIEW.md)
argued (Finding 5) for splitting the bytes-exist vocabulary in two — `skipped`
for content withheld where the survivors still join, and something else for
content removed where they do not — so that a filter's duty would be decidable
from one file. `0.16` did not take it, on the grounds that the reason word must
not decide the duty. The consequence is worth knowing when reading the checker:
`undecoded-skipped` and `filtered-decoded` are byte-shaped alike — a
bytes-class region between two adjacent units — and one owes a Discontinuity
while the other does not. Nothing here can tell them apart, and nothing is
missing that could.

## Conformance vectors

The specification ships 53 hand-built vectors, vendored verbatim into
[`tests/vectors/`](https://github.com/adamkjonsson/python-zipline/tree/main/tests/vectors)
and run by `tests/test_vectors.py` across the three tiers — `accept` (a
conformant file, with its expected JSONL projection), `reject` (structural
corruption), and `isolate` (a semantic violation the reader must not pass
silently).

`0.16` adds a key rather than a fourth tier: an `accept` entry marked
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
repository. Two that were defective upstream have since been fixed there;
`VECTOR-DEFECTS.md` is the closed record.

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

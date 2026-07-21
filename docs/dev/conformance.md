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
| Semantic (single-pass) | declare-before-use, id uniqueness, session lifetime, per-participant `seq_start` order, file-kind purity, reserved-flag bits, `prim:` payload widths | `conformance.py` (`ConformanceChecker`) | `SemanticError` — isolate or reject |
| Sequenced order | a SEQUENCED session's stored order really is a causal linearization | `order.py` `verify_sequenced` / `SessionReader.verify()` | needs the merge algorithm |
| Decode coverage | every input offset decoded or marked Undecoded, never both | `transform.py` `check_coverage` | whole-file property |

The last two are deliberately **out of the single-pass checker's reach**: one
needs the k-way merge to decide, the other is a property of the whole file
against a second file. The `ConformanceChecker` docstring calls this boundary
out explicitly.

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

Two design points worth preserving when editing it:

- **Bounded memory.** Per-session state is freed at each Session End; only the
  set of ended session ids is retained (to police the nothing-after-Session-
  End rule). The checker runs on unbounded streams.
- **Consistent-on-raise.** Each handler runs its pure checks *before* mutating
  state or locking the file kind, so a raised violation leaves the checker
  consistent and a lenient reader can isolate the offending block and carry
  on.

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
raised under `strict=True`. Truncation is a third, expected condition. This is
the [errors page](../user/errors.md)'s subject, from the reader's side.

## Going beyond the standard

Per `CLAUDE.md`, support must stay complete *and* must not silently exceed the
v1.0 spec: any behavior beyond the standard has to be flagged to the user with
an explicit callout. As of this version nothing does — the checker's rules are
the spec's, and the two out-of-band checks (sequenced order, coverage) are
spec requirements enforced elsewhere, not extensions. A new rule that isn't in
v1.0 does not belong in the `ConformanceChecker`.

## Where to go next

- [Architecture](architecture.md) — where these checkers sit among the layers.
- [Errors and diagnostics](../user/errors.md) — the exception tiers from the
  consumer's side.
- [Testing](testing.md) — `test_conformance.py` and the golden/merge tests
  that lock this behavior down.

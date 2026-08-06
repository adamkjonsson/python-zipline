# Architecture

`zpf` is built as **four layers**, each one public and each built on the one
below. The design goal: the easy path (top layers) writes only conformant
files and reads any conformant file, while a tool that needs exact control
over bytes drops one layer down without leaving the package.

```
        ┌──────────────────────────────────────────────┐
Layer 4 │ transforms + CLI      transform.py  cli.py   │
        ├──────────────────────────────────────────────┤
Layer 3 │ ergonomic reader/     reader.py    writer.py │
        │ writer                                       │
        ├──────────────────────────────────────────────┤
Layer 2 │ block I/O             binary.py    jsonl.py  │
        ├──────────────────────────────────────────────┤
Layer 1 │ blocks                blocks.py    _frame.py │
        └──────────────────────────────────────────────┘
          support (cross-cutting): order.py  conformance.py
```

## Module map

| Module | Layer | Responsibility |
| ------ | ----- | -------------- |
| `_frame.py` | 1 | Low-level byte machinery: little-endian primitives, 4-byte alignment, TLV option encoding, and `RawOption` (the raw preservation of unrecognized options). |
| `blocks.py` | 1 | The typed block model — frozen dataclasses mirroring the spec 1:1 (`FileHeader`, `Source`, `Session`, `Record`, …), plus `Span`, `Origin`, and the flag/kind/role enums. `UnknownBlock` carries block types the model doesn't recognize. |
| `binary.py` | 2 | `BlockReader`/`BlockWriter` over the binary container: framing, the structural error tier, truncation handling. |
| `jsonl.py` | 2 | The JSON-Lines projection: `JsonlReader`/`JsonlWriter` and the lossless `binary_to_jsonl`/`jsonl_to_binary` converters, using the same dataclasses. |
| `reader.py` | 3 | The session-first reader: `open`, `FileReader`, `SessionReader`; one indexing pass, then lazy per-record access. |
| `writer.py` | 3 | The ergonomic, always-conformant writer: `create` and its handle objects (`SessionWriter`, `ParticipantHandle`, …). |
| `transform.py` | 4 | File→file transforms: `merge_files` (two directions → one sequenced pass-through), `rewrite_decoded` (filter/reorder), and the validators — `check_coverage` against the input, `check_extents` from the derived file alone, `check_splice` across a pair of stages. |
| `cli.py` | 4 | The `zpf` console script: `info`, `cat`, `convert`, `validate`, `merge`. |
| `order.py` | support | RFC 1982 serial arithmetic and the streaming causal merge, used by the reader's `timeline()`, the writer's order check, and the merge transform. |
| `conformance.py` | support | `ConformanceChecker` — the semantic tier as a single-pass observer, used by the writer (always) and available standalone; plus `CoverageLedger`, which gathers what a file says about each input stream and rules on it at end-of-stream. |

Everything is re-exported at the package top level: consumers write
`zpf.open`, `zpf.Record`, `zpf.merge_files`, and never import a submodule.

## Data flow

**Write** — `create()` → a `SessionWriter` builds typed blocks and feeds each
one through the `ConformanceChecker`, then to a `BlockWriter` (binary) or
`JsonlWriter` (jsonl). Misuse fails here, at write time, not in some other
tool's reader.

**Read** — `open()` runs one pass of a `BlockReader`/`JsonlReader`, building a
lightweight index (sessions → participants → record offsets). `SessionReader`
then reads each record from the stream lazily; `timeline()` either replays a
SEQUENCED session's stored order or runs the streaming merge on the fly.

**Transform** — `merge_files()` opens two `FileReader`s, runs `causal_merge`
from `order.py`, and re-emits through a `FileWriter`. `check_coverage()` reads
a decode stage and its input and reconciles spans against Undecoded markers.

## Design invariants

These shape the whole codebase; a change that breaks one is almost certainly
wrong.

- **Preservation is a MUST, designed in at the bottom.** Unrecognized options
  survive as `RawOption`, unknown block types as `UnknownBlock`; both
  round-trip byte-faithfully. A newer file passes through an older `zpf`
  intact. See [Handle imperfect files](../user/howto/robustness.md).
- **Writers enforce conformance at write time.** The semantic tier is checked
  as blocks are produced, so the ergonomic writer *cannot* emit a
  nonconformant file. See [Conformance strategy](conformance.md).
- **Readers isolate semantic errors but reject structural ones.** A
  well-framed block that breaks a rule is isolated (a diagnostic, in lenient
  mode); a corrupt byte stream is fatal (`StructuralError`, always). See
  [Errors and diagnostics](../user/errors.md).
- **One canonical encoding.** The writers emit a single canonical byte layout
  (option order, padding), which is what makes the round-trip guarantees
  byte-exact for files `zpf` produced.
- **Declare-on-first-use.** Ids are handed back as handles, so a record can
  never reference a session, participant, or source that was never declared.

## Where to go next

- [Conformance strategy](conformance.md) — where each class of spec rule is
  enforced.
- [Testing](testing.md) — the test suite that pins these invariants down.
- The [API reference](../api/index.md) — every public module in detail.

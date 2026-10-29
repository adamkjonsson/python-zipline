# Architecture

`zpf` is built as **four layers**, each one public and each built on the one
below. The design goal: the easy path (top layers) writes only conformant
files and reads any conformant file, while a tool that needs exact control
over bytes drops one layer down without leaving the package.

```
        ┌───────────────────────────────────────────────────────┐
Layer 4 │ stages, transforms   decode.py  sessionize.py         │
        │ and CLI              transform.py  cli.py             │
        ├───────────────────────────────────────────────────────┤
Layer 3 │ ergonomic reader/    reader.py  reassembly.py         │
        │ writer               writer.py                        │
        ├───────────────────────────────────────────────────────┤
Layer 2 │ block I/O            binary.py  jsonl.py              │
        ├───────────────────────────────────────────────────────┤
Layer 1 │ blocks               blocks.py  _frame.py  content.py │
        └───────────────────────────────────────────────────────┘
          support (cross-cutting): order.py  conformance.py
                                   errors.py  _intervals.py
```

## Module map

| Module | Layer | Responsibility |
| ------ | ----- | -------------- |
| `_frame.py` | 1 | Low-level byte machinery: little-endian primitives, 4-byte alignment, TLV option encoding, and `RawOption` (the raw preservation of unrecognized options). |
| `blocks.py` | 1 | The typed block model — frozen dataclasses mirroring the spec 1:1 (`FileHeader`, `Source`, `Session`, `Record`, …), plus `Span`, `InputExtent`, and the flag, kind, layer and adjacency enums. `UnknownBlock` carries block types the model doesn't recognize. |
| `content.py` | 1 | The `content_type` label: its grammar (`ContentType`), the normative `prim:` decode, the closed `net:` vocabulary, and `ContentRegistry`, the caller-supplied dispatch for what the format leaves advisory. Works on `str` and `bytes` only, so the block layer can build on it. |
| `binary.py` | 2 | `BlockReader`/`BlockWriter` over the binary container: framing, the structural error tier, truncation handling. |
| `jsonl.py` | 2 | The JSON-Lines projection: `JsonlReader`/`JsonlWriter` and the lossless `binary_to_jsonl`/`jsonl_to_binary` converters, using the same dataclasses. |
| `reader.py` | 3 | The session-first reader: `open`, `FileReader`, `SessionReader`; one indexing pass, then lazy per-record access. |
| `reassembly.py` | 3 | The decoder's view of one participant stream: `StreamView`, with `segments()`/`chunks()` for a byte stream, `datagrams()` for a packet one and `units()` for a unit sequence, plus each layer's offset space (`record_ranges`, `stream_extent`), the FIN positions inside a transport extent that coverage owes nothing (`fin_positions`, since `0.24`), and the timestamps a cited range implies (`ts_for`, `ts_first_for`). |
| `writer.py` | 3 | The ergonomic, always-conformant writer: `create` and its handle objects (`SessionWriter`, `ParticipantHandle`, …), and `derive_from`, the scaffolding a derived file starts from. |
| `decode.py` | 4 | `decode_stage`, the decode-stage orchestrator: one output participant per input stream, under the same ids. Also the parts both stages share: `InputStream`, and the coverage ledger that auto-fills what no record cited. |
| `sessionize.py` | 4 | `sessionize_stage`, the sessionization stage that fans one packet stream out into one session per flow (`FlowSession`), with fresh ids, on the shared core in `decode.py`. |
| `transform.py` | 4 | File→file transforms: `merge_files` (two directions → one sequenced pass-through), `rewrite_decoded` (filter/reorder), and the validators — `check_coverage` against the input, `check_extents` from the derived file alone, `check_splice` across a pair of stages. |
| `cli.py` | 4 | The `zpf` console script: `info`, `cat`, `convert`, `validate`, `merge`. |
| `order.py` | support | RFC 1982 serial arithmetic, the walks that unwrap sequence numbers into offsets (`_Placer`) and acks into positions (`_AckWalk`), a record's end by the sequence numbers it occupies, and the streaming causal merge. Used by the reader's `timeline()` and `verify()`, the writer's order check, the merge transform and `reassembly.py`. |
| `conformance.py` | support | `ConformanceChecker` — the semantic tier as a single-pass observer, used by the writer (always) and available standalone; plus `CoverageLedger`, which gathers what a file says about each input stream and rules on it at end-of-stream. |
| `errors.py` | support | The exception hierarchy, all `ZpfError`s: `StructuralError`, `SemanticError` (with `AdvisoryError` for a writer-only MUST), `TruncatedError`, `EncodeError`, and `ContentError`. |
| `_intervals.py` | support | Interval arithmetic for coverage: `complement` and `intersections` over sorted ranges. |

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

**Stage** — `decode_stage()` and `sessionize_stage()` open the input as a
`FileReader` and the output with `create()`, hand the caller the input's
streams as `InputStream`s, and count every citation and marker in a shared
ledger. On a clean close the ledger auto-fills what nothing cited and each
output session ends declaring its inputs' extents. A decode stage mirrors its
input's sessions; a sessionization stage creates its sessions as the caller
finds flows.

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

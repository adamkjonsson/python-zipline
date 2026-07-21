# Errors and diagnostics

The specification splits reader-side error handling into tiers, and `zpf`
mirrors that split in one exception hierarchy plus one value type,
{class}`~zpf.Diagnostic`. The guiding rule: a reader rejects a file only when
the byte stream itself can no longer be trusted. A single bad *block* is
isolated, not fatal; a stream that simply *ends early* is an expected
condition, not an error at all.

## The exception hierarchy

Everything the package raises descends from {class}`~zpf.ZpfError`, so
`except zpf.ZpfError` catches all of it:

```
ZpfError
├── StructuralError   the byte stream is corrupt — reject the file
├── SemanticError     a well-framed block breaks a MUST — isolate it
├── TruncatedError    the stream ended inside a block (strict mode only)
└── EncodeError       a value can't be represented when writing
```

### StructuralError: reject the file

Raised when framing is broken and nothing after the fault can be trusted: a
bad or missing magic, a File Header that is absent or not first, an
unimplemented `version_major`, `tick_hz == 0`, a block length that is not a
multiple of 4, or a length field that overruns its block. There is no
"isolate and continue" here — the file is rejected. This is the one tier
that is fatal **regardless** of `strict`.

### SemanticError: isolate the offending block

Raised when a block is well-framed but its *content* violates a
specification MUST (an out-of-order SEQUENCED session, a record naming a
participant that was never declared, a coverage overlap). By default a
reader does **not** raise these: it isolates the offending unit and records
a {class}`~zpf.Diagnostic` instead, so the rest of the file stays readable.
Pass `strict=True` to escalate the first such violation to a raised
`SemanticError`.

### TruncatedError: the stream ended early

A file that ends inside a block is the normal signature of a live or crashed
writer, so truncation is treated as an **expected condition**, not
corruption. By default the reader stops cleanly at the last whole block,
sets its status attributes, and emits a `truncated` diagnostic — it does not
raise. `TruncatedError` is raised only under `strict=True`. See
[Handle imperfect files](howto/robustness.md).

### EncodeError: a value won't fit the wire

Raised on the **writing** side, at block construction or serialization: an
integer out of range, an option value over 65 535 bytes, a zero `tick_hz`,
or a `Custom` payload whose length is not a multiple of 4. It signals a bug
in the producing code, caught before a malformed byte reaches the file.

## Strict versus lenient reads

{func}`zpf.open` and {class}`~zpf.FileReader` default to **lenient**: collect
diagnostics, keep going. `strict=True` flips semantic violations and
truncation into raised exceptions on first sight — useful in tests and
pipelines that must fail loudly. Structural corruption always raises, either
way.

```python
import zpf

# Lenient (default): findings become diagnostics, the read completes.
with zpf.open("maybe-truncated.zpf") as reader:
    if reader.truncated:
        print("stopped early; got", sum(s.record_count for s in reader.sessions()))

# Strict: the first truncation or semantic violation raises.
try:
    with zpf.open("maybe-truncated.zpf", strict=True) as reader:
        reader.sessions()[0].records()
except zpf.TruncatedError as exc:
    print("rejected:", exc)          # -> stream ends inside a block's content (offset 284)
```

## Working with `Diagnostic`

A {class}`~zpf.Diagnostic` is the frozen record of one non-fatal condition
noticed while reading. Every lenient finding is one of these, available as
`reader.diagnostics`:

| Field | Meaning |
| ------ | ------- |
| `offset` | Byte offset in the file where the condition was detected. |
| `category` | Short machine-readable tag — branch on this. |
| `message` | Human-readable description for logs and the CLI. |

```python
with zpf.open("suspect.zpf") as reader:
    for diagnostic in reader.diagnostics:
        print(f"{diagnostic.offset}: {diagnostic.category}: {diagnostic.message}")
```

Categories you will meet:

| Category | Source | Meaning |
| -------- | ------ | ------- |
| `truncated` | reader | The stream ended inside a block. |
| `trailing-bytes` | reader | Bytes followed the End block. |
| `nonconformant` | reader | A block violated a conformance rule (the isolated `SemanticError`). |
| `coverage-gap` | {func}`zpf.check_coverage` | An input range neither decoded nor marked Undecoded. |
| `coverage-overlap` | {func}`zpf.check_coverage` | An input range both decoded and marked Undecoded. |
| `coverage-excess` | {func}`zpf.check_coverage` | A cited range past the input stream's extent. |

The `zpf validate` command is a thin wrapper over these diagnostics: it
prints each one and turns their presence into exit code `1`. See the
[validate how-to](howto/validate.md) and the [CLI reference](cli.md).

## Where to go next

- [Handle imperfect files](howto/robustness.md) — truncation, unknown
  blocks, and lenient reads in practice.
- [Validate a file](howto/validate.md) — driving these diagnostics from the
  command line and in code.
- [Concepts: robustness](concepts.md#robustness-truncation-unknown-blocks-and-errors)
  — where this tiering comes from in the format.
- The [API reference](../api/errors.md) for the exact exception definitions.

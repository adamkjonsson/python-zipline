# Handle imperfect files

Real captures are cut short, and files written by newer tools carry blocks
this version doesn't know. The format is designed so neither is fatal: a
reader stops cleanly at a truncation and preserves anything it doesn't
understand. This page shows how to read such files deliberately — the practice.
The [errors page](../errors.md) has the underlying model, and [Faces and
I/O](../guides/faces-and-io.md#imperfect-input) places these rules in the
wider I/O story (they hold identically on both faces).

## Truncated files

A file that ends inside a block is the normal signature of a live or crashed
writer, so a **lenient** read (the default) does not raise. It stops at the
last whole block, keeps every record before the cut, and tells you what
happened through status attributes and a diagnostic:

```python
import zpf

with zpf.open("truncated.zpf") as reader:
    print("complete:", reader.complete)     # False
    print("truncated:", reader.truncated)   # True
    for session in reader.sessions():
        print("records recovered:", session.record_count)
    for diagnostic in reader.diagnostics:
        print(diagnostic.category, "-", diagnostic.message)
        # -> truncated - stream ends inside a block's content
```

Read `reader.truncated` (or `reader.complete`) to branch on it; the records
that *did* arrive are fully usable. The command line shows the same status:

```console
$ zpf info truncated.zpf
note: truncated: stream ends inside a block's content
face:      binary
kind:      raw
complete:  False  (truncated)
...
```

If instead you want truncation to be an error — in a test, or a pipeline that
must reject partial files — read with `strict=True` and catch
{class}`~zpf.TruncatedError`:

```python
try:
    with zpf.open("truncated.zpf", strict=True) as reader:
        reader.sessions()[0].records()
except zpf.TruncatedError as exc:
    print("rejected:", exc)   # -> ... inside a block's content (offset 284)
```

## Unknown blocks are preserved

The format reserves room for block types and options this library version
predates. A reader does not choke on them and does not discard them: it keeps
each unknown block's type and content, and each unrecognized option's bytes,
so they survive being written back out. That is what makes the [round
trip](convert.md#the-round-trip-guarantee) lossless even for a file produced
by a newer tool — convert it to JSONL and back and the unknown blocks return
intact (byte-for-byte when the input was canonically encoded, as `zpf`'s own
output always is). So an old `zpf` can faithfully relay a file it only partly
understands, rather than silently dropping the parts it doesn't.

## Structural corruption still stops

Preservation applies to content a reader can *frame* but not *interpret*.
When the framing itself is broken — a bad magic, a length that overruns its
block, a missing File Header — the byte stream can no longer be trusted and
the reader raises {class}`~zpf.StructuralError` regardless of `strict`. This
is the one condition there is no lenient path through: the file is rejected.
See [Errors: StructuralError](../errors.md#structuralerror-reject-the-file).

## Choosing a mode

| You want… | Use |
| --------- | --- |
| Recover what's readable, inspect the rest | Lenient (default): check `reader.truncated` and `reader.diagnostics`. |
| Fail on any truncation or semantic violation | `zpf.open(..., strict=True)`, catch {class}`~zpf.ZpfError`. |
| A pass/fail exit code from the shell | [`zpf validate`](validate.md) (`--strict` to stop at the first fault). |

## Where to go next

- [Faces and I/O](../guides/faces-and-io.md) — the feature this recipe belongs
  to, including the block-level readers that handle a live tail or a pipe.
- [Errors and diagnostics](../errors.md) — the exception tiers and the
  `Diagnostic` value in full.
- [Concepts: robustness](../concepts.md#robustness-truncation-unknown-blocks-and-errors)
  — why the format draws the line where it does.
- [Validate a file](validate.md) — turning these conditions into findings.

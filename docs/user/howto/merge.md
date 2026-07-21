# Merge two captured directions

When the two directions of a conversation are captured at *different* tap
points, each file holds one direction and the two clocks can drift apart —
so sorting by timestamp can put a response before its request. The **merge
transform** fixes this once: it interleaves the two directions into their
true causal order and writes a single [SEQUENCED
pass-through](../concepts.md#sequenced-sessions) file, so every downstream
reader replays the stored order and never pays for ordering again. Tutorial
[stage 3](../tutorial.md#3-causal-order) motivates the problem; this guide is
the task.

## On the command line

```console
$ zpf merge sideA.zpf sideB.zpf -o merged.zpf
$ zpf info merged.zpf
face:      binary
kind:      pass-through
...
source 0: zpf-input sideA.zpf
source 1: zpf-input sideB.zpf
session 0: proto=tcp key='...' participants=[...] records=2 sequenced end=yes
```

`--produced-by "tool 1.2"` overrides the provenance stamped on the output
header (it defaults to `zpf <version>`).

## In Python

{func}`zpf.merge_files` takes the two inputs, the output, and a required
`produced_by`:

```python
import zpf

zpf.merge_files("sideA.zpf", "sideB.zpf", "merged.zpf", produced_by="tutorial 1.0")

with zpf.open("merged.zpf") as reader:
    (session,) = reader.sessions()
    assert session.sequenced
    session.verify()   # confirms the stored order is a valid linearization
```

`produced_at` (Unix seconds) and `creator` are optional header fields;
`produced_at` defaults to now.

## What the inputs must be

The merge handles the canonical **two-tap** case exactly, and reports
anything else as a {class}`~zpf.ZpfError` rather than guessing. Each input
must be:

- a **raw** file (not a decode stage or an already-merged file),
- holding **exactly one session** with **exactly one participant** (one
  captured direction), and
- sharing the other's **clock** (`tick_hz` and time epoch).

If your captures don't fit this shape — more than one session per file, say —
split them first so each merge sees one direction per input.

## What the output is

A pass-through file that preserves the inputs faithfully:

- Each input is declared as a `zpf-input` **source**, with a `sha256` digest
  when the input was a path — so a consumer can confirm the input hasn't
  changed.
- The output session carries the **SEQUENCED** flag, and its records are
  stored in causal order.
- Every participant's `origin` points back to its input stream.
- Records are re-emitted **byte-identically** — payloads, timestamps,
  `seq_start`/`ack` hints, flags, and unknown options all preserved. Only
  `spans` are stripped, because a pass-through file's provenance is its
  `origin` plus the preserved offsets.

Because the digest is recorded, you can later re-validate the merge against
its inputs; see [Validate a file](validate.md#in-python) and
{func}`zpf.check_coverage`.

## Where to go next

- [Concepts: sequenced sessions](../concepts.md#sequenced-sessions) — what
  the flag guarantees and why it's worth baking in.
- [Validate a file](validate.md) — `zpf validate merged.zpf --verify`
  re-checks the stored order.
- The [API reference](../../api/transform.md) for {func}`zpf.merge_files` and
  the causal-merge internals.

# Convert between binary and JSONL

Every `.zpf` file has [two faces](../concepts.md#two-faces-of-one-model): the
compact **binary** container and a **JSON-Lines** projection that is easy to
read, diff, and pipe through `jq`. Converting between them is lossless in
both directions. This is the recipe; the [faces and I/O
guide](../guides/faces-and-io.md) is the feature — which face to pick, and the
block layer the converters are built from.

## On the command line

```console
$ zpf convert session.zpf session.jsonl        # binary -> jsonl
$ zpf convert session.jsonl session2.zpf       # jsonl -> binary
```

With no `--to`, the target face is the opposite of the input's. Pass `--to`
to force a face regardless of the input — handy for normalizing a directory
of mixed files:

```console
$ zpf convert maybe-either.zpf out.jsonl --to jsonl
```

To dump JSONL to stdout instead of a file — to eyeball it or pipe it — use
`zpf cat`, which is `convert --to jsonl` writing to your terminal:

```console
$ zpf cat session.zpf | jq 'select(.type=="record") | .ts'
```

## In Python

{func}`zpf.binary_to_jsonl` and {func}`zpf.jsonl_to_binary` do the same
conversions on paths or open streams:

```python
import zpf

zpf.binary_to_jsonl("session.zpf", "session.jsonl")
zpf.jsonl_to_binary("session.jsonl", "session2.zpf")
```

Both stream block by block, so a multi-gigabyte capture converts in constant
memory — no payload is held longer than it takes to rewrite it.

## The round-trip guarantee

Conversion is **semantically lossless**: every field value survives a trip to
the other face and back — including blocks and options the library doesn't
understand, whose type and content are carried through, not dropped. See
[Handle imperfect files](robustness.md#unknown-blocks-are-preserved).

For a file that `zpf` wrote, that guarantee is the stronger one the
[tutorial](../tutorial.md#4-the-two-faces) shows: the round trip reproduces
the **original bytes exactly**.

```python
from pathlib import Path
import zpf

zpf.binary_to_jsonl("session.zpf", "session.jsonl")
zpf.jsonl_to_binary("session.jsonl", "roundtrip.zpf")

assert Path("roundtrip.zpf").read_bytes() == Path("session.zpf").read_bytes()
```

```{note}
Byte-for-byte equality holds because this library always writes the format's
**canonical** encoding. A non-canonically encoded input — hand-built, or from
another tool — comes back canonicalized: same values, possibly different bytes.
[Faces and I/O](../guides/faces-and-io.md#what-lossless-guarantees) explains
which details the JSONL face does not pin down, and why digests are defined
over the binary form.
```

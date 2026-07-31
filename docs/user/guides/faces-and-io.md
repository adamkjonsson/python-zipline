# Faces and I/O

A `.zpf` file has **two faces**: the compact binary container that the
specification makes canonical, and a JSON-Lines projection of exactly the same
model. This guide covers choosing between them, converting losslessly in either
direction, and the flat block-level I/O underneath the session-first reader —
including how both faces behave on imperfect input. For the normative account of
the split see [Concepts](../concepts.md#two-faces-of-one-model); for the terse
recipes, [convert](../howto/convert.md) and
[handle imperfect files](../howto/robustness.md).

## One model, two encodings

Both faces encode the same typed blocks ({mod}`zpf.blocks`), so nothing about
the *data* changes when you switch — only the bytes on disk:

- **Binary** — length-prefixed typed blocks carrying TLV options, 4-byte
  aligned. Compact, seekable, streamable, and the face digests are defined
  over. This is the default everywhere and the right choice for anything you
  keep.
- **JSON-Lines** — one JSON object per line, payloads base64-encoded. Readable,
  diffable, and `jq`-able. Meant for debugging, tests, and small captures; it is
  bigger (base64 alone adds a third to every payload) and its lines have to be
  parsed in order.

The rule of thumb: **store binary, look at JSONL.** Keep captures in the binary
face and project to JSONL when a human or a text tool needs to see inside.

## Choosing a face when reading

You usually don't. {func}`zpf.open` sniffs the face — the binary magic at
offset 8, or a leading `{` for JSONL — so reading code is face-agnostic:

```python
with zpf.open("capture.zpf") as reader:
    print(reader.face)      # "binary" or "jsonl"
```

Pass `face="binary"` / `face="jsonl"` to force one (and reject the other), or
call {func}`zpf.detect_face` when you only want to know what you have without
opening it:

```python
if zpf.detect_face(path) == "jsonl":
    ...
```

```{note}
The two faces differ in cost, not in behaviour. On the binary face `zpf.open`
indexes block *offsets* and re-reads payloads on demand, so memory stays
proportional to the record count. The JSONL face has no framing to seek by, so
it is parsed eagerly into memory — fine for the debugging captures it is meant
for, wasteful for a multi-gigabyte one.
```

## Choosing a face when writing

{func}`zpf.create` takes the same `face=` argument and defaults to `"binary"`.
The sink must match: a binary stream for the binary face, a text stream for
JSONL (a path works for either).

```python
with zpf.create("capture.jsonl", tick_hz=1_000_000, face="jsonl") as writer:
    ...
```

Everything above the encoder — sessions, participants, records, the conformance
checks — is identical on both faces, so a test can write JSONL, assert against
readable lines, and the production path can write binary from the same code.

## Converting

{func}`zpf.binary_to_jsonl` and {func}`zpf.jsonl_to_binary` convert whole files,
on paths or open streams, and return the diagnostics collected on the way:

```python
import zpf

diagnostics = zpf.binary_to_jsonl("capture.zpf", "capture.jsonl")
zpf.jsonl_to_binary("capture.jsonl", "roundtrip.zpf")
```

Both stream block by block, so conversion runs in constant memory whatever the
file's size — no payload is held longer than it takes to rewrite it. Pass
`strict=True` to escalate recoverable issues on either end into exceptions.

The command line does the same job: `zpf convert in out` (with `--to` to force
a target face) and `zpf cat file.zpf` to dump JSONL to stdout — see the
[convert how-to](../howto/convert.md).

### What "lossless" guarantees

Conversion is **semantically lossless in both directions**: every field value
survives the trip, including blocks and options this library version doesn't
understand, which are carried through rather than dropped
([why](../howto/robustness.md#unknown-blocks-are-preserved)).

Byte-for-byte identity is the stronger claim, and it holds for any file `zpf`
wrote, because this library always emits the format's canonical encoding. A
*non-canonically* encoded binary — hand-built, or from another tool — comes back
**canonicalized**: same values, possibly different bytes, since the JSONL face
does not pin down padding, option order, or how `spans` are chunked. Digests are
defined over the binary form, so compute them there.

### The four escapes

The binary face has one rule for anything a reader does not recognise: skip it
by its stated length, keep it, and do not error. The projection mirrors that
rule exactly, so **every** unrecognised element has a defined syntactic form —
never invented, never silently dropped:

| Unrecognised | JSONL form |
|---|---|
| option id | an entry in the block's `options` array, under its real id |
| block type | `{"type": "0x0042", "content": "<base64 of the whole content>"}` |
| enum value | the raw number in place of the string label |
| flag bit | a hex token, e.g. `"flags": ["psh", "0x0020"]` |

A hex token is `0x` plus exactly four hex digits, which is unambiguous against
every defined `type` string and flag token because those are all words. None of
this is an error path — it is the normal behaviour that lets a file written
against a later version survive a round-trip through an older converter. The
unknown-block escape is the one case that is *byte*-exact rather than merely
semantically lossless, precisely because a converter cannot take apart a layout
it does not know.

Preserving is not interpreting: a reserved flag bit round-trips as a hex token
while still being ignored semantically, exactly as an unknown option id is
retained but not acted on.

Unknown **keys** on a known block are the one case with no escape, by design.
There is no option id to write, and guessing one would manufacture data — so
such a key is dropped with a diagnostic, or raises under `strict=True`.

## Dropping to the block layer

{func}`zpf.open` and {class}`~zpf.FileWriter` are the session-first API: they do
the demultiplexing and bookkeeping. Underneath sits a flat, one-block-at-a-time
layer that mirrors the specification directly —
{class}`~zpf.BlockReader`/{class}`~zpf.BlockWriter` for binary,
{class}`~zpf.JsonlReader`/{class}`~zpf.JsonlWriter` for JSONL. The two pairs have
the same shape, which is what makes the converters four lines long.

Reach for it when the session view is the wrong shape for the job:

- **The source isn't seekable.** `zpf.open` needs to seek; a pipe, a socket, or
  a live tail doesn't. The block readers are single-pass and bounded-memory, so
  they handle a growing file or `stdin`.
- **You want blocks, not sessions** — counting block types, rewriting a file
  block by block, or relaying a file you only partly understand.

```python
with zpf.BlockReader(sys.stdin.buffer) as reader:
    for block in reader:
        if isinstance(block, zpf.Record):
            handle(block.payload)
```

Both readers are context managers, expose `header`, `complete`, `truncated`, and
`diagnostics`, and close only the streams they opened themselves.

## Imperfect input

Both faces follow the same two-tier rule, so the choice of face never changes
whether a file is readable:

- **Truncation is expected.** A file cut mid-block (binary) or mid-line (JSONL)
  is read up to the last complete unit; `truncated` goes True, `complete` stays
  False, and every block before the cut is fully usable.
- **Unknown means skip — and this library also preserves.** Unknown block types
  and unrecognized options round-trip intact, in both directions.
- **Structural corruption stops.** Broken framing — bad magic, a length that
  overruns its block, a missing File Header — raises
  {class}`~zpf.StructuralError` regardless of `strict`, because the byte stream
  can no longer be trusted.

Everything recoverable is reported through `diagnostics` rather than raised;
`strict=True` (on `zpf.open`, on the block readers, and on the converters) turns
those reports into exceptions instead. The
[robustness how-to](../howto/robustness.md) has the practice, including a table
for picking a mode, and [Errors and diagnostics](../errors.md) has the model.

## Advanced

### Block pipelines

Because a reader and a writer of either face trade the same typed blocks, a
filter is a loop. This drops one session's records from a file while leaving
everything else — including blocks this version doesn't understand — untouched:

```python
with zpf.BlockReader("in.zpf") as reader, zpf.BlockWriter("out.zpf") as writer:
    for block in reader:
        if isinstance(block, zpf.Record) and block.session_id == drop_id:
            continue
        writer.write(block)
```

Mixing the pairs converts as a side effect: read binary, write JSONL. The
writers enforce only *well-formedness* — File Header first, one header, nothing
after the End block — and deliberately not semantic conformance, so a tool can
re-emit imperfect data faithfully. Pass `check=True` to opt into full
conformance checking on write:

```python
with zpf.BlockWriter("out.zpf", check=True) as writer:
    ...
```

Note that a filter like the one above can break conformance in ways the framing
layer won't catch — dropping a Session block while keeping records that
reference it, for instance. `check=True` is how you find out.

### One block at a time

For the JSONL face, {func}`zpf.dumps_block` and {func}`zpf.loads_block` convert a
single block to and from one line, and
{func}`~zpf.jsonl.block_to_obj`/{func}`~zpf.jsonl.obj_to_block` stop at the
JSON-object level if you want to inspect or patch the mapping first. All four
take an `on_issue` callback that fires for any value with no representation on
the other face:

```python
line = zpf.dumps_block(record)
same = zpf.loads_block(line)
```

These are the primitives the JSONL reader and writer are built from — useful for
tests, fixtures, and log lines, not for whole files.

## See also

- [Convert between binary and JSONL](../howto/convert.md) — the recipe and the
  round-trip guarantee.
- [Handle imperfect files](../howto/robustness.md) — truncation, unknown blocks,
  and choosing strict or lenient.
- [Tutorial: the two faces](../tutorial.md#4-the-two-faces) — the round trip,
  hands-on.
- [Reading files](reading.md) — the session-first reader this layer sits under.
- [Concepts: two faces](../concepts.md#two-faces-of-one-model) and
  [robustness](../concepts.md#robustness-truncation-unknown-blocks-and-errors).
- API reference: [`zpf.binary`](../../api/binary.md),
  [`zpf.jsonl`](../../api/jsonl.md), [`zpf.blocks`](../../api/blocks.md).

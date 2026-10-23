# Reading files

This guide is the read side: opening a `.zpf` and navigating it — files,
sessions, participants, records, and the causal timeline. Everything is lazy, so
a multi-gigabyte capture costs no more to open than a tiny one. For a first
hands-on pass see [tutorial stage 2](../tutorial.md#2-read-it-back); for the
data model, [Concepts](../concepts.md#what-a-zpf-file-holds).

## Opening a file

{func}`zpf.open` returns a {class}`~zpf.FileReader` and works as a context
manager. It accepts a path or an already-open, **seekable** binary/text stream,
and auto-detects the face —
[binary or JSON-Lines](../concepts.md#two-faces-of-one-model):

```python
with zpf.open("cap.zpf") as reader:
    print(reader.face)                       # e.g. "binary"
    print(reader.stream_kind(7, 0))          # e.g. (SourceKind.CAPTURE, OutputLayer.TRANSPORT)
    print(reader.header.tick_hz)
```

The reader exposes the file's top-level facts as plain attributes: `header`,
`sources`, `decoders`, `undecoded`, and — for spotting an incomplete or
malformed file — `complete`, `truncated`, and `diagnostics` (covered in the
[robustness how-to](../howto/robustness.md)). By default a read is **lenient**: a
nonconformant or truncated file still opens, collecting findings rather than
raising. Pass `strict=True` to raise on the first violation instead, or
`check=False` to skip the semantic checks altogether for a file whose
conformance you already know — roughly 30% of the cost of opening one, and
what you give up is spelled out in the
[robustness how-to](../howto/robustness.md#declining-the-checker).

A pipe or live tail isn't seekable; for those, drop to the lower-level
{class}`~zpf.BlockReader` / {class}`~zpf.JsonlReader`, which stream block by
block.

## Sessions and participants

A file holds one or more **sessions** (one conversation each), and each session
holds one or more **participants** (the endpoints). Reach them with
{meth}`~zpf.FileReader.sessions` (all of them) or
{meth}`~zpf.FileReader.session` (by id):

```python
with zpf.open("cap.zpf") as reader:
    for session in reader.sessions():
        print(session.session_id, session.proto, session.key, session.sequenced)
        for participant in session.participants:
            print(" ", participant.participant_id, participant.endpoint)
```

A {class}`~zpf.SessionReader` also carries `end` (how the session closed, if
stated) and `record_count`. Sessions are demultiplexed for you — even when the
file interleaves records from several sessions on disk, each session view yields
only its own.

## Iterating records

Three iterators give the same records in different orders; pick by what you need:

- {meth}`~zpf.SessionReader.records` — the session's records in **stored** order
  (for a sequenced session, that already *is* the causal order).
- {meth}`~zpf.SessionReader.stream` — one participant's records in stream order,
  no cross-participant merge.
- {meth}`~zpf.SessionReader.timeline` — the session's records in **causal**
  order, merging the participants when the session isn't sequenced. Usually the
  one you want; see the [ordering guide](ordering.md).

```python
for record in session.timeline():
    print(record.timestamp, record.sender_pid, record.payload[:40])
```

`record.payload` is always the bytes as stored. When a record labels what those
bytes *are* with a `content_type`, {meth}`Record.content
<zpf.blocks.Record.content>` reads them through the label — `prim:u32` becomes
an `int` — and {meth}`FileReader.content <zpf.reader.FileReader.content>` adds
your handlers for the advisory `mime:`/`dec:` schemes. See
[reading payload content](decoding.md#reading-payload-content).

To consume a participant's bytes as reassembled application streams rather than
raw records, use {meth}`~zpf.SessionReader.reassemble` — see the
[decoding guide](decoding.md).

## Laziness, and what it costs

`zpf.open` is **lazy in payloads and eager in structure**. One indexing pass
runs on open and keeps a locator per record; record payloads are then read
from the file as you iterate, and are not retained. So iterators are
independent — hold one per session and interleave them freely — and
re-reading a session costs the same as reading it the first time. Just
remember the reader is a context manager, and a lazy iterator stops working
once the file is closed.

What the index costs, measured with 64-byte payloads on the binary face:

| | Retained on open |
| --- | --- |
| Per record | ~50 bytes |
| Per session | ~1.9 kB |
| **Per byte of payload** | **nothing** |

So memory is flat in the *size* of a file and linear in its *block count*:
100,000 records cost about 5 MB, and a file of 10,000 short sessions costs
more than one of 100,000 records in a single session. That is the trade the
eager index buys — independent iterators, `record_count` without a walk, and
O(1) random access through {meth}`~zpf.SessionReader.ranges`.

**The JSON-Lines face is eager in payloads too**, and holds the whole file:
a text stream cannot be seeked back to a block, so there is nothing for a
locator to point at and every block is kept in memory. Expect roughly
**384 bytes per record** rather than 50 — 19 MB for 50,000 records. `zpf.open`
picks the face by sniffing, so this applies whenever what you opened turns out
to be JSONL, which {attr}`~zpf.FileReader.face` reports.

### Reading in bounded memory

For input whose size you do not control — an unbounded capture, a pipe, a live
tail — the session-first reader is the wrong tool, and no option makes it the
right one. Drop to {class}`~zpf.BlockReader` or {class}`~zpf.JsonlReader`,
which yield one block at a time in constant memory and need no seeking:

```python
with zpf.BlockReader("huge.zpf") as blocks:
    for block in blocks:
        if isinstance(block, zpf.Record):
            handle(block.payload)        # constant memory, whatever the size
```

What you give up is everything the index provides: no sessions, no
participants, no reassembly, no `timeline()` — you are reading a block stream
and keeping whatever state you need yourself, dropping each session's at its
{class}`~zpf.SessionEnd`, exactly as the format's
[streamable design](../concepts.md#streamable-by-construction-declare-on-first-use)
intends. [Faces and I/O](faces-and-io.md) covers both readers.

That design is a statement about the **format** — a reader *may* build its
tables incrementally and drop a session's state at its Session End. `zpf.open`
deliberately does not take that option: it reads the whole structure up front
so that everything above can be random-access and independent. The two
statements are easy to read as one promise, and they are not.

## See also

- [Tutorial: read it back](../tutorial.md#2-read-it-back) — the same steps,
  hands-on.
- [Ordering](ordering.md) — `timeline()` and the causal-order machinery.
- [Decoding](decoding.md) — consuming streams as reassembled application bytes.
- [Faces and I/O](faces-and-io.md) — which face you opened, and the block-level
  readers for sources `zpf.open` can't seek.
- [Handle imperfect files](../howto/robustness.md) — the recipe for truncation,
  diagnostics, and choosing strict or lenient.
- [Concepts](../concepts.md) — the data model behind sessions, participants, and
  records.
- API reference: [`zpf.reader`](../../api/reader.md).

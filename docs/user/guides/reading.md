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
    print(reader.face, reader.file_kind)   # e.g. "binary", "raw"
    print(reader.header.tick_hz)
```

The reader exposes the file's top-level facts as plain attributes: `header`,
`sources`, `decoders`, `undecoded`, and — for spotting an incomplete or
malformed file — `complete`, `truncated`, and `diagnostics` (covered in the
[robustness how-to](../howto/robustness.md)). By default a read is **lenient**: a
nonconformant or truncated file still opens, collecting findings rather than
raising. Pass `strict=True` to raise on the first violation instead.

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

To consume a participant's bytes as reassembled application streams rather than
raw records, use {meth}`~zpf.SessionReader.reassemble` — see the
[decoding guide](decoding.md).

## Laziness

The reader indexes block boundaries on open but reads record *payloads* only as
you iterate, straight from the file. Iterators are independent — you can hold one
per session and interleave them — and nothing buffers the whole file, so memory
stays flat regardless of size. This is the read-side half of the format's
[streamable design](../concepts.md#streamable-by-construction-declare-on-first-use):
just remember the reader is a context manager, and a lazy iterator stops working
once the file is closed.

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

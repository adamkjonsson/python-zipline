# Concepts

This page is the mental model behind the Zipline Payload Format and this
library. The [tutorial](tutorial.md) walks you through the API; this page
explains *why* the format looks the way it does, and defines the vocabulary
the rest of the documentation uses. Deep links go to the
[v0.12 specification](https://github.com/adamkjonsson/zipline/blob/v0.12/docs/zipline-payload-format.md),
which is normative for this library.

## What a `.zpf` file holds

A `.zpf` file stores the *payload* output of a network sessionizer: the bytes
that flowed between endpoints once packets have been reassembled into
sessions, plus the metadata needed to consume them. It is not a packet
capture — retransmissions, out-of-order segments, and overlaps have already
been resolved by the producer. What remains is clean application data with
enough context (who sent it, when, and in what order) to replay or analyze
it.

Three nesting levels organize that data:

```
File
└── Session            (id, protocol, key, metadata)
    ├── Participant     (local id, endpoint/identity, per-side metadata)
    ├── Participant
    └── Record …        (sender participant, timestamp, payload, ordering hints)
```

A **session** is one conversation: a TCP connection, a chat room, a one-way
UDP feed. A file holds any number of them.

A session has **N participants**, not two hard-coded "sides". Both directions
of a TCP connection is the `N = 2` case; a five-person chat room is `N = 5`;
a one-way multicast feed is `N = 1`.

A **record** is one directed payload unit: it names its session, the
**sender** participant, a timestamp, the payload bytes, and ordering hints.
It names no recipient — a record is implicitly addressed to every *other*
participant in its session.

In this library, {func}`zpf.open <zpf.reader.open>` gives you a
{class}`~zpf.reader.FileReader`; its sessions are
{class}`~zpf.reader.SessionReader` objects exposing participants and records.
{func}`zpf.create <zpf.writer.create>` is the writing counterpart.

## Streamable by construction: declare-on-first-use

The nesting above is logical, not physical. On disk a file is a flat sequence
of typed blocks, and the only ordering rule is that a descriptor (session,
participant, source, decoder) must appear **before the first block that
references it**. Nothing is listed up front, so:

- a writer can emit a session the moment its first packet arrives, add a
  participant when a fourth person joins the chat mid-capture, then
  flush-and-forget a finished session (marking it with a *Session End*
  block) — all in bounded memory on unbounded input;
- a reader builds its tables incrementally as it reads, and may drop a
  session's state at its Session End.

A file *may* end with an *End* block; if present, the file is known
complete. A file without one (a live stream, a crashed writer) is still
valid — see [Robustness](#robustness-truncation-unknown-blocks-and-errors).

The library's writer enforces declare-on-first-use for you: handles returned
by {meth}`~zpf.writer.FileWriter.begin_session` and
{meth}`~zpf.writer.SessionWriter.participant` carry the ids, so you cannot
reference something that has not been declared.

## Time: ticks on a capture clock

Timestamps are integers on a **packet-time clock**, never wall clock. The
file header fixes a resolution, `tick_hz` (ticks per second — `1_000_000`
for microseconds), and an optional origin, `time_epoch` (ticks since the
Unix epoch, default 0). A record's `timestamp` is in ticks since that
origin, so wall time, when you need it, is
`(time_epoch + timestamp) / tick_hz` seconds since the Unix epoch.

Capture time keeps replay deterministic: live and offline runs of the same
traffic order identically, regardless of when the file was written.

One file-level flag qualifies the clock: `SINGLE_CLOCK` asserts that every
record in the file was stamped against *one trustworthy clock*, so
timestamps are comparable across sessions and sources. It matters for
sequencing sessions that have no better ordering signal — see the next
section.

## Ordering: why timestamps are not enough

When the two directions of a TCP connection are captured separately (one
file per tap point), their clocks can be skewed, and merging by timestamp
can put an answer *before* the question it answers. TCP's sequence and
acknowledgement numbers give a clock-independent fix: a segment from B
carrying `ack = N` proves B had already received A's stream up to byte `N`
when it was built. That is a causal *happens-before* edge that holds no
matter what either clock says.

Records therefore carry the absolute `seq_start`/`ack` values from the wire
as **ordering hints**. From them the consumer can derive a partial order — a
DAG of causal edges — and needs timestamps only to break ties between
genuinely concurrent records. Because writers must store each participant's
records already sorted by `seq_start`, resolving the order is a cheap
streaming merge, never a sort.

The library gives you three levels of involvement:

- {meth}`SessionReader.timeline() <zpf.reader.SessionReader.timeline>` —
  the one you normally want: yields the session's records in causal order,
  running the streaming merge only when needed (see `SEQUENCED` below).
- {meth}`SessionReader.stream() <zpf.reader.SessionReader.stream>` — one
  participant's records in stream order, no merge involved.
- {func}`zpf.causal_merge <zpf.order.causal_merge>` and friends in
  {mod}`zpf.order` — the raw machinery, if you need the partial order
  itself.

### Sequenced sessions

Files are read more often than written, so a producer that has already
resolved the order may *bake it in*: store the records so that file order
is a valid causal linearization and set the **`SEQUENCED`** flag on the
session. Readers of such a session consume records in stored order and skip
the merge entirely. The flag is per-session — a file may mix sequenced and
unsequenced sessions — and the ordering hints stay present, so a reader can
still verify the stored order ({meth}`~zpf.reader.SessionReader.verify`,
or `zpf validate --verify` on the command line).

A session with no seq/ack hints (chat, one-way UDP) has no causal edges, so
its sequenced order rests purely on timestamps — which is only sound under
one trustworthy clock. That is the link to `SINGLE_CLOCK` above: a producer
must not mark a hint-less session `SEQUENCED` without it (or an equivalent
per-session clock guarantee).

## File kinds: raw, decode stage, pass-through

Every `.zpf` file is exactly one of three kinds, told by what its records
reference:

```
tap.pcap ──[ sessionizer ]──▶ raw.zpf ──[ http decoder ]──▶ decoded.zpf
                                                             (decode stage)
sideA.zpf ─┐
           ├─[ merge ]──▶ merged.zpf
sideB.zpf ─┘              (pass-through)
```

- A **raw** file is capture-sourced: its records are *byte runs* — chunks of
  the reassembled stream, with boundaries wherever reassembly happened to
  produce them — referencing a *capture* source (a pcap file, an
  interface).
- A **decode stage** is derived from another `.zpf` by a decoder. Its
  records are *decoder-imposed units* (an HTTP message, a TLS record) whose
  boundaries follow application semantics, not transport chunking.
  {func}`zpf.decode_stage <zpf.decode.decode_stage>` writes one end to end:
  it consumes the input through reassembly views and fills in the provenance
  and coverage bookkeeping for you.
- A **pass-through** file is derived from other `.zpf` files by a
  byte-preserving transform — the spec defines one, the **merge**, which
  combines separately-captured directions into one file with sequenced
  sessions ({func}`zpf.merge_files <zpf.transform.merge_files>`, or
  `zpf merge`). Payload bytes, offsets, and ordering hints pass through
  unchanged.

Two facts classify any record: whether it carries a `decoder_id` (present ⇔
decoded), and the `kind` of the source it references (`capture` ⇔ raw,
`zpf-input` ⇔ derived). A derived file is never a mix of decoded and
pass-through records. {attr}`FileReader.file_kind
<zpf.reader.FileReader.file_kind>` reports the kind.

Raw and decoded views live in **separate files** because their boundaries
rarely align — one HTTP message can start and end mid-way through raw
records. Chaining is uniform: `raw → tls-records → http` is the same
file → file mechanism applied twice.

## Typing a payload: `content_type`

A record may label what its payload *is*, as `scheme:value`. The label is
metadata *about* the bytes, never a replacement for them — the format's rule
is that **the bytes always stay the source of truth**. Three schemes exist,
and the format defines them to very different depths:

| Scheme | What the format settles |
| ------ | ----------------------- |
| `prim:<token>` | **Everything.** A closed vocabulary (`u8`…`u64`, `i8`…`i64`, `bytes`), little-endian byte order, signedness from the token's `u`/`i`, and a width that MUST equal `payload_len`. |
| `mime:<type>` | Only that the value is an IANA media type. Nothing about turning the bytes into a value. |
| `dec:<token>` | Nothing: a type **private to the record's decoder**, meaning whatever that decoder documents. |

A `dec:` token is namespaced by the producing decoder's **`name`** — not its
id, and not its version. `dec:request` from a decoder named `http/1.1` and
`dec:request` from one named `smtp` are two unrelated types, and a consumer
must resolve the token *through the record's `decoder_id` to that decoder's
name* before attaching any meaning to it. This is why the label alone is not
enough to interpret a record: {meth}`Record.content
<zpf.blocks.Record.content>` handles the self-contained `prim:` scheme, while
`dec:` needs the file — {meth}`FileReader.content
<zpf.reader.FileReader.content>`, which can do that lookup.

Two fallback rules follow from bytes-as-truth, and both are normative:

- **An unknown scheme is opaque** — not an error. The payload is simply bytes.
- **A `prim:` width that disagrees with `payload_len` MUST be treated as
  unknown**, and the reader "MUST NOT pad, truncate, or reinterpret". Emitting
  such a record is a *writer* violation, so `zpf` refuses to write one; on
  read it is reported as a diagnostic and the record is kept intact (see
  [`AdvisoryError`](errors.md#advisoryerror-a-writer-only-must)).

Because `mime:` and `dec:` mean nothing the format defines, interpreting them
is a **caller-supplied** claim: you register handlers in a
{class}`~zpf.ContentRegistry`. The [decoding guide](guides/decoding.md#reading-payload-content)
shows both halves — labelling on write, interpreting on read.

## Provenance: spans, coverage, origins

A derived file must say where its bytes came from.

Each input `.zpf` is declared as a source of kind `zpf-input`, with a
content `digest` — the dependency edge that lets a consumer confirm the
derived file still matches its input.

A decoded record cites its input bytes as a **span set**: references of the
form `{source_id, session_id, pid, off_start, off_end}` into the input's
participant streams. Offsets are **logical stream offsets** — 0-based
positions in the reassembled application stream, counting missing bytes
(gaps) as if present — not record ids and not TCP sequence numbers. Byte 0
is the stream's first application byte (anchored at `isn + 1` when the TCP
handshake was seen). A decoder consumes those streams through
{meth}`SessionReader.reassemble <zpf.reader.SessionReader.reassemble>`,
which works in these offsets directly and can {meth}`cite
<zpf.reassembly.StreamView.cite>` a range without the arithmetic.

What a decoder could *not* parse it must say out loud: an **Undecoded**
block names an input range and a reason (`undecodable`, `gap`,
`truncated`) instead of silently dropping it. Together this yields the
**coverage guarantee**: every offset of every input stream is either covered
by some decoded record's spans or marked Undecoded — checkable with
{func}`zpf.check_coverage <zpf.transform.check_coverage>`.

Pass-through files carry no spans; instead every participant maps back to
its input stream with a single **origin** reference, and offset preservation
does the rest.

## Two faces of one model

The canonical encoding is a **framed binary container**: length-prefixed
typed blocks, each carrying TLV options. A documented **JSON-Lines
projection** — one JSON object per line, payloads base64-encoded — is the
easily-consumed face for debugging and small captures. Both encode the same
model, and conversion is lossless in both directions (`zpf convert`, or
{func}`zpf.binary_to_jsonl <zpf.jsonl.binary_to_jsonl>` /
{func}`zpf.jsonl_to_binary <zpf.jsonl.jsonl_to_binary>`).

{func}`zpf.open <zpf.reader.open>` detects the face automatically, so
reading code never cares which one it was handed.

## Robustness: truncation, unknown blocks, and errors

The format plans for imperfect files, and the library mirrors its rules:

- **Truncation is expected, not corruption.** A file that stops mid-block
  (crashed or still-writing producer) is read up to the last complete
  block; the partial tail is discarded. Only a file ending in a valid End
  block is *known* complete.
- **Unknown means skip, and this library also preserves.** Unknown block
  types, unknown TLV options, and reserved fields are the format's
  extension mechanism — readers skip them without complaint. This library
  goes further and round-trips them byte-faithfully
  ({class}`~zpf.blocks.UnknownBlock`, {class}`~zpf.RawOption`), so passing
  a file through `zpf` never loses what a newer or vendor tool wrote.
- **Two error tiers.** *Structural* problems (bad magic, an overrunning
  length) poison the byte stream — the reader rejects the file with
  {class}`~zpf.errors.StructuralError`. *Semantic* violations (a record
  referencing an undeclared session, a duplicate id) are isolated instead:
  the reader can drop the offending block or session and reports what it
  did via {class}`~zpf.errors.Diagnostic` objects. Data never vanishes
  silently. See [Errors and diagnostics](errors.md).
- **Some violations bind only the writer.** Reserved `flags` bits, and a
  `prim:` label its payload contradicts, leave a reader nothing to act on —
  the format tells it to ignore the bits or the label and use the block. So
  `zpf` refuses to *write* such a block, but reading one keeps it and merely
  reports the finding ({class}`~zpf.errors.AdvisoryError`): the alternative
  would discard well-framed data over metadata nobody consults.

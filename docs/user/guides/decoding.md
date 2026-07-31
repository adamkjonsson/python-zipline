# Decoding

A **decode stage** turns a raw `.zpf` — records that are transport byte-runs —
into one whose records are whole application messages (an HTTP request, a TLS
record, a JSON body). This guide explains the two halves of that job: reading
an input stream, and writing the decoded output. For a runnable walkthrough see
the [decoder tutorial](../tutorial-decoding.md); for the terse recipe, the
[decode-stage how-to](../howto/decode_stage.md); for the normative model behind
spans and coverage, [Concepts](../concepts.md#provenance-spans-coverage-origins).

## Reading the input: reassembly views

A decoder consumes its input one **participant stream** at a time. The raw
records are byte-runs whose boundaries came from reassembly, not the
application, so a message can span several records or share one with the next.
{meth}`~zpf.SessionReader.reassemble` hands back one {class}`~zpf.StreamView`
per participant and does that bookkeeping for you: it works in **logical stream
offsets** (0-based positions in the reassembled stream, byte 0 = the first
application byte) so you never touch `seq_start`/`isn` arithmetic.

```python
with zpf.open("rest_raw.zpf") as reader:
    for session in reader.sessions():
        for stream in session.reassemble():
            ...  # one StreamView per participant
```

### Stream-oriented vs packet-oriented

The transport decides how a stream is shaped, and the view reflects it —
{attr}`~zpf.StreamView.is_stream_oriented` tells the two apart:

- A **TCP** stream is a continuous *byte stream*. Contiguous records coalesce
  into {class}`~zpf.Segment` runs from {meth}`~zpf.StreamView.segments`, and a
  lost segment surfaces as an explicit {class}`~zpf.Gap`. The decoder
  reassembles messages out of the byte runs.
- A **UDP** flow — or any stream with no sequence hints — is a sequence of
  whole *datagrams*. You iterate {meth}`~zpf.StreamView.datagrams` (one
  {class}`~zpf.Datagram` per packet, cumulative offsets, no reassembly and no
  gaps). There is nothing to coalesce, so the byte-stream methods raise on a
  packet stream and point you to `datagrams()`.

```python
for stream in session.reassemble():
    if stream.is_stream_oriented:
        for segment in stream.segments():
            parse(segment.data)        # contiguous bytes starting at segment.off_start
    else:
        for datagram in stream.datagrams():
            parse(datagram.data)       # one whole message
```

### Segments, chunks, and gaps

For a byte stream, three methods offer different views of the same bytes:

- {meth}`~zpf.StreamView.segments` — the contiguous runs only. The usual choice:
  a decoder parses each run and cites offsets within it.
- {meth}`~zpf.StreamView.chunks` — the runs **and** the {class}`~zpf.Gap`s
  between them, in order. Use it when a hole matters — e.g. to mark the gap
  explicitly, or to reset parser state across it.
- {meth}`~zpf.StreamView.reassembled` — the whole stream as one `bytes`. A
  convenience for the gap-free case; it raises if the stream has a gap, since
  joining across a hole would misalign every offset after it.

A `Gap` occupies logical offset space (the coordinate system counts missing
bytes as if present), so offsets stay stable whether or not a segment was lost —
which is what lets a citation mean the same thing across decode stages.

## Writing the decode stage

{func}`zpf.decode_stage` is the orchestrator. It opens the input, copies its
`tick_hz`/`time_epoch` onto the output header, declares the input as a
`zpf-input` source (hashing it for the digest), declares the decoder, and
re-declares each input participant under the *same* id — then hands back one
{class}`~zpf.DecodeStream` per input stream, pairing it with the output
participant that stands for it.

```python
from datetime import UTC, datetime

with zpf.decode_stage(
    "rest_raw.zpf", "rest_decoded.zpf",
    decoder=("http/1.1", "1.0"),        # name, version
    produced_by="http-decode 1.0",
    produced_at=datetime.now(tz=UTC),   # or int Unix seconds
    proto="http",
) as dec:
    for stream in dec.streams():
        for segment in stream.segments():
            for start, end, kind in split_messages(segment.data):
                dec.record(
                    stream, segment.data[start:end], ts=segment.ts,
                    content_type=f"dec:http-{kind}",
                    cites=(segment.off_start + start, segment.off_start + end),
                )
```

Two details carry weight:

- **`ts=`** is the record's time. Per the timestamp rule a reassembled unit's
  time is the *completion* time of the last input record it came from — the
  run's {attr}`Segment.ts <zpf.Segment.ts>` — so it is passed explicitly rather
  than guessed.
- **`cites=(off_start, off_end)`** mints the {class}`~zpf.Span` for you, filling
  in the input's `session_id`/`participant_id` from `stream`. A record can only
  ever cite the stream it was decoded from, which is impossible to get wrong by
  hand. Pass a ready `Span` (or several) if you cite more than one range.

To keep your own {func}`zpf.create` call instead of the orchestrator, use
{meth}`zpf.FileWriter.derive_from`, which builds the same source + participant
scaffolding and returns it, without owning the loop.

## Coverage is handled for you

The **coverage guarantee** is that within each input stream every offset is
either cited by a decoded record or named by an {class}`~zpf.Undecoded` block —
never both, never neither (see
[Concepts](../concepts.md#provenance-spans-coverage-origins)). On a clean close
`decode_stage` closes that loop: it marks every byte you left uncited as
Undecoded, with a `reason`:

- **`gap`** for a reassembly hole (a {class}`~zpf.Gap` — no bytes exist
  anywhere upstream);
- **`skipped`** for data the decoder simply passed over (the bytes *do* exist
  upstream, recoverable through the provenance chain).

`undecodable` is deliberately **not** used by auto-fill — it is the decoder's
own claim that it *tried and could not parse* a region, so it is reserved for an
explicit {meth}`~zpf.DecodeStage.undecoded` call. Auto-fill covers only what is
left and raises rather than overriding an explicit marker. Pass
`fill_undecoded=False` to opt out and mark everything yourself;
{func}`~zpf.check_coverage` then reports a `coverage-gap` for anything missed —
with auto-fill on it returns `[]` by construction.

## Reading payload content

The write side above labelled each record with `content_type=` — a decoded
record says what it *is*. This is the read side of that label: turning a
payload into a value instead of re-implementing the same `int.from_bytes` or
`json.loads` in every consumer. The model behind the label, including the
`dec:` namespace rule, is in
[Concepts](../concepts.md#typing-a-payload-content_type).

### `prim:` — built in, always

{meth}`Record.content <zpf.blocks.Record.content>` interprets the one scheme
the format defines completely. Integers are little-endian and signed iff the
token starts with `i`; you never spell that out:

```python
for record in session.records():
    print(record.content_type, record.content())
    # prim:u32   -> 1234
    # prim:i8    -> -1
    # prim:bytes -> b"..."
```

Everything else falls back to the payload bytes, **untouched** — that is the
format's own rule, not a shortcut. You get the raw payload for a record with
no label, for a `mime:`/`dec:`/unknown scheme, and for a `prim:` label the
payload contradicts (an illegal token, or a width that disagrees with
`payload_len`): the bytes are never padded, truncated, or reinterpreted to fit
a label. Such a file is nonconformant and `zpf` will not *write* one, but it
reads back with the record intact and a `nonconformant` diagnostic — see
[`AdvisoryError`](../errors.md#advisoryerror-a-writer-only-must).

### `mime:` and `dec:` — your handlers, via a registry

```{warning}
**Beyond the standard.** The format defines `mime:` only as "an IANA media
type" and `dec:` as a type private to the record's decoder. It says nothing
about turning either one's bytes into a Python value, so `zpf` ships **no**
interpretation of them: a {class}`~zpf.ContentRegistry` supplies the
*dispatch*, and what a label means is your handler's claim. `prim:` is the
opposite case — fully normative, built in, and never routed through a
registry.
```

Register a handler per media type, or per (decoder **name**, token), and pass
the registry to {func}`zpf.open`. Then
{meth}`FileReader.content <zpf.reader.FileReader.content>` dispatches:

```python
import json

registry = zpf.ContentRegistry()
registry.register_mime("application/json", json.loads)
registry.register_dec("http/1.1", "request", parse_http_request)

with zpf.open("rest_decoded.zpf", content=registry) as reader:
    for record in reader.session(0).records():
        value = reader.content(record)     # dict, your parsed request, int, or bytes
```

Four things worth knowing:

- **`dec:` needs the file**, which is why this method lives on the reader: the
  token's namespace is the *decoder's name*, and a record carries only a
  `decoder_id`. The reader resolves one to the other. A record with no
  `decoder_id` — or whose decoder declared no `name` — cannot have a `dec:`
  token resolved at all, and falls back to bytes.
- **Media types match without their parameters**, case-insensitively, as IANA
  defines them: one `"text/plain"` handler serves
  `mime:text/plain; charset=utf-8`. Decoder names and `dec:` tokens match
  exactly.
- **A handler's exceptions reach you unchanged.** A handler that fails is a bug
  or corrupt input, not a fallback condition, so the library will not quietly
  hand back bytes and hide it.
- **With no registry, `reader.content(record)` is exactly `record.content()`** —
  the same normative behaviour, no special cases.

### Insisting on an interpretation

Both methods return `bytes` for two different reasons: `prim:bytes` means "the
value *is* these bytes", and the fallback means "the label could not be
honoured". When that difference matters — a pipeline that must not silently
accept an unusable label — pass `strict=True` and the fallback raises
{class}`~zpf.ContentError` (a `ZpfError` *and* a `ValueError`) instead:

```python
try:
    value = reader.content(record, strict=True)
except zpf.ContentError as exc:
    log.warning("unusable label: %s", exc)   # e.g. requires payload_len 4, got 5
```

## Advanced

### More than one decoder in a session

A record carries its own `decoder_id`, so a single session can mix decoders —
useful when one stream layers protocols, or a flow carries several. Declare each
decoder up front and select it per record:

```python
with zpf.decode_stage(raw, sink, decoder=("http/1.1", "1.0"),
                      produced_by="d 1.0", produced_at=datetime.now(tz=UTC)) as dec:
    http = dec.decoder                        # the stage default
    json = dec.writer.add_decoder("json/1.0")  # a second decoder
    for stream in dec.streams():
        for segment in stream.segments():
            dec.record(stream, headers, ts=segment.ts, cites=...)               # → http
            dec.record(stream, body, ts=segment.ts, cites=..., decoder=json)    # → json
```

The same `decoder=` override is available on {meth}`~zpf.DecodeStage.undecoded`,
so an undecoded region can be attributed to the decoder that declined it. Only
the `fill_undecoded` auto-fill stays on the stage default — it has no basis to
pick a specific decoder per region.

### Decoder chaining

Chaining is the same file-to-file mechanism applied more than once:
`raw → tls-records → http` is two decode stages, each taking the previous
output as its input. Because every decoded record cites the input it was built
from, provenance recurses: a consumer walking back from an HTTP record reaches
the TLS-record stream, then the raw capture. Each stage is an ordinary
`decode_stage` run; nothing special is needed to chain them.

## See also

- [Decoder tutorial](../tutorial-decoding.md) — the same ideas as a runnable,
  end-to-end example.
- [Write decode-stage files](../howto/decode_stage.md) — the task-shaped recipe.
- [Read payloads as typed values](../howto/payload_content.md) — the recipe for
  the registry.
- [Concepts: provenance](../concepts.md#provenance-spans-coverage-origins) — the
  normative model for spans, coverage, and origins.
- [Concepts: typing a payload](../concepts.md#typing-a-payload-content_type) —
  what each `content_type` scheme does and does not settle.
- API reference: [`zpf.reassembly`](../../api/reassembly.md) (stream views),
  [`zpf.decode`](../../api/decode.md) (the orchestrator),
  [`zpf.content`](../../api/content.md) (labels and the registry), and
  [`zpf.transform`](../../api/transform.md) ({func}`~zpf.check_coverage`).

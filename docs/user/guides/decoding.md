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

- **`tcp-gap`** for a reassembly hole (a {class}`~zpf.Gap` — no bytes exist
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
- [Concepts: provenance](../concepts.md#provenance-spans-coverage-origins) — the
  normative model for spans, coverage, and origins.
- API reference: [`zpf.reassembly`](../../api/reassembly.md) (stream views),
  [`zpf.decode`](../../api/decode.md) (the orchestrator), and
  [`zpf.transform`](../../api/transform.md) ({func}`~zpf.check_coverage`).

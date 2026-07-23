# Write decode-stage files

A **decode stage** is a file derived from a raw one by a decoder: its records
are whole application messages (an HTTP request, a JSON response) instead of
the transport byte-runs the raw file holds. This is the recipe; the
[decoder tutorial](../tutorial-decoding.md) is the full worked walkthrough,
built on the same example.

## What makes a file a decode stage

Three things, all enforced by the
{class}`~zpf.conformance.ConformanceChecker`:

1. **Provenance on the header.** `produced_by` and `produced_at` — required
   the moment a file becomes derived.
2. **A `zpf-input` source** citing the raw predecessor, ideally with a
   `digest` so a consumer can confirm the input hasn't changed.
3. **A `decoder=` on every record.** A record is *decoded* precisely because
   it names the decoder that produced it.

{func}`zpf.decode_stage` sets up all three from the input and hands back a
per-stream decode context:

```python
from datetime import UTC, datetime

import zpf

with zpf.decode_stage(
    "rest_raw.zpf", "rest_decoded.zpf",
    decoder=("http/1.1", "tutorial"),   # name, version
    produced_by="http-decode 1.0",
    produced_at=datetime.now(tz=UTC),   # or int Unix seconds
    proto="http",
) as dec:
    for stream in dec.streams():        # one DecodeStream per input stream
        for segment in stream.segments():
            for start, end, kind in split_messages(segment.data):
                dec.record(
                    stream, segment.data[start:end], ts=segment.ts,
                    content_type=f"dec:http-{kind}",
                    cites=(segment.off_start + start, segment.off_start + end),
                )
```

`decode_stage` opens the input, copies its `tick_hz`/`time_epoch`, declares
the `zpf-input` source (hashing the input for the digest), declares the
decoder, and re-declares each input participant under the same id. To keep
your own {func}`zpf.create` call instead, {meth}`zpf.FileWriter.derive_from`
builds the same scaffolding without owning the loop.

The loop above uses {meth}`~zpf.DecodeStream.segments` because HTTP rides
TCP, a byte stream. For a **UDP** flow — or any stream without sequence
hints — iterate {meth}`~zpf.DecodeStream.datagrams` instead: one whole
{class}`~zpf.Datagram` per packet, no reassembly and no gaps.
{attr}`~zpf.DecodeStream.is_stream_oriented` tells the two apart, and the
byte-stream methods raise on a packet stream:

```python
for stream in dec.streams():
    if stream.is_stream_oriented:
        for segment in stream.segments():
            ...  # split segment.data into messages, as above
    else:
        for datagram in stream.datagrams():  # each datagram is one message
            dec.record(stream, datagram.data, ts=datagram.ts,
                       cites=(datagram.off_start, datagram.off_end))
```

## Cite input bytes with spans

Each decoded record carries a **span**: the `[off_start, off_end)` range of
the input stream its bytes came from, in **logical stream offsets** (0-based
positions in the reassembled stream, where byte 0 is the first application
byte). Passing `cites=(off_start, off_end)` to {meth}`~zpf.DecodeStage.record`
mints the {class}`~zpf.Span` for you, filling in the input's
`session_id`/`participant_id` (which name the stream in the *input's*
namespace, not the decoded file's) — so a citation can only ever name the
stream it came from. One message reassembled from several input records still
carries a single covering span. See
[Provenance](../concepts.md#provenance-spans-coverage-origins).

## Coverage is handled for you

A decoder must never *silently* drop input. The **coverage guarantee** is
that within each input stream, every offset is either covered by a decoded
record's span or named by an {class}`~zpf.Undecoded` block — never both,
never neither.

On a clean exit `decode_stage` marks every byte you left uncited as
Undecoded: `skipped` for data the decoder passed over, `tcp-gap` for a
reassembly hole. So the guarantee holds without a single `undecoded()` call.
When the decoder wants to make its own stronger claim — `undecodable` means
it *tried and could not parse* the bytes, which auto-fill never asserts on
your behalf — mark it explicitly:

```python
dec.undecoded(stream, 48, 77, reason="undecodable")
#                     ^off_start/off_end (logical offsets of this stream)
```

Auto-fill covers only what's left, and raises rather than overriding an
explicit marker. Pass `fill_undecoded=False` to opt out entirely and mark
everything yourself.

## Check the coverage guarantee

{func}`zpf.check_coverage` verifies it after the fact, returning the
violations (empty when it holds) — with auto-fill on, it is `[]` by
construction:

```python
findings = zpf.check_coverage("rest_decoded.zpf", "rest_raw.zpf")
assert findings == []
```

Or from the command line, the way you'd gate a decoder's output in a
pipeline:

```console
$ zpf validate rest_decoded.zpf --input rest_raw.zpf
rest_decoded.zpf: OK
```

A missing marker surfaces as a `coverage-gap`; a redundant one as a
`coverage-overlap`. See [Validate a
file](validate.md#reading-the-diagnostics).

## Where to go next

- [Tutorial: writing a decoder](../tutorial-decoding.md) — the same steps as
  a runnable, end-to-end example.
- [Concepts: file kinds](../concepts.md#file-kinds-raw-decode-stage-pass-through)
  and [provenance](../concepts.md#provenance-spans-coverage-origins) — the
  normative model, including how decode stages chain (`raw → tls-records →
  http`).
- The API reference for [`zpf.decode`](../../api/decode.md),
  [`zpf.reassembly`](../../api/reassembly.md), and
  [`zpf.transform`](../../api/transform.md) ({func}`zpf.check_coverage`).

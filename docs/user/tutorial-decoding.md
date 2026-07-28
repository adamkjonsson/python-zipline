# Tutorial: writing a decoder

The [main tutorial](tutorial.md) captured and read *raw* files: records
that are byte runs straight off the wire. This companion tutorial takes the
next step — turning a raw file into a **decode stage**, where records are
whole application messages instead of transport chunks. We'll decode a small
REST call: one HTTP `GET` and its JSON response.

A decode stage is a separate file derived from the raw one by a decoder:

```
rest.pcap ──[ sessionizer ]──▶ rest_raw.zpf ──[ http decoder ]──▶ rest_decoded.zpf
```

This is exactly the shape [Concepts](concepts.md#file-kinds-raw-decode-stage-pass-through)
calls a decode stage. It assumes you've done the main tutorial; the three
scripts here live under
[`docs/user/examples/`](https://github.com/adamkjonsson/python-zipline/tree/main/docs/user/examples)
and run with plain `python`.

## 1. The raw input a decoder consumes

A decoder reads its input one **participant stream** at a time.
{meth}`~zpf.SessionReader.reassemble` hands back one {class}`~zpf.StreamView`
per stream and turns its byte-run records — whose boundaries came from
reassembly, not the application — into contiguous {class}`~zpf.Segment`
runs. The decoder parses those runs, never the raw `seq_start`/`isn`
arithmetic. Offsets are **logical stream offsets**: 0-based positions in the
reassembled stream, where byte 0 is the stream's first application byte
(`isn + 1` when the TCP handshake was seen). Where a lost segment left a
hole, `chunks()` surfaces it as an explicit {class}`~zpf.Gap`, so a decoder
never silently welds across missing bytes.

Streams come in two shapes, and the view reflects the transport:

- A **TCP** stream (this REST call) is a continuous *byte stream*.
  {attr}`~zpf.StreamView.is_stream_oriented` is true; contiguous records
  coalesce into {class}`~zpf.Segment` runs via
  {meth}`~zpf.StreamView.segments` / {meth}`~zpf.StreamView.chunks`, and a
  lost segment shows up as a {class}`~zpf.Gap`. A message can span several
  records or share one with the next, so the decoder reassembles.
- A **UDP** flow — or any stream with no sequence hints — is instead a
  sequence of whole *datagrams*, each a self-contained message.
  `is_stream_oriented` is false; you iterate {meth}`~zpf.StreamView.datagrams`
  (one {class}`~zpf.Datagram` per packet, with cumulative offsets — no
  reassembly, no gaps). There is nothing to coalesce, so the byte-stream
  methods raise on a packet stream and point you to `datagrams()`.

Checking `is_stream_oriented` is how a general decoder picks the right
idiom; this HTTP decoder knows its input is TCP and goes straight to
`segments()`.

```{literalinclude} examples/05_rest_raw_input.py
:language: python
```

```console
$ python 05_rest_raw_input.py
participant 0 (10.0.0.1:51000):
  offset   0: b'GET /users/1 HTTP/1.1\r\nHost: api.example.com\r\n'
  offset  46: b'Accept: application/json\r\n\r\n'
  reassembled [0, 74)
participant 1 (93.184.216.34:80):
  offset   0: b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 30\r\n\r\n{"id":1,"name":"Ada Lovelace"}'
  reassembled [0, 101)
```

The request arrived as two records (at offsets `0` and `46`); `segments()`
coalesced them into the single run `[0, 74)` the decoder parses. Those
logical offsets are the coordinate system every span in the next step is
written in.

## 2. Write the decode stage

The decoder reassembles each stream, splits it into HTTP messages, and
emits one record per message. Three things make a file a decode stage, all
enforced by the {class}`~zpf.conformance.ConformanceChecker`:

- `produced_by`/`produced_at` on the header — required the moment a file
  becomes derived;
- a `zpf-input` **source** citing the raw file, with a `digest` so a
  consumer can confirm the input hasn't changed;
- `decoder=` on each record — a record is *decoded* precisely because it
  names the decoder that produced it.

{func}`zpf.decode_stage` does all three for you: it copies the input's time
base onto the output header, declares the `zpf-input` source (hashing the
input for the digest), declares the decoder, and re-declares each input
participant under the *same* id — then hands back one {class}`~zpf.DecodeStream`
per input stream. You write only the loop that turns segments into records.

Each record carries a **span**: the `[off_start, off_end)` range of the
input stream its bytes came from, in the logical offsets from step 1. Rather
than build one by hand, pass `cites=(off_start, off_end)` to
{meth}`~zpf.DecodeStage.record` — it mints the {class}`~zpf.Span` with the
input's `session_id`/`participant_id` filled in (those name the stream in the
*input's* namespace, not the decoded file's), so you cannot accidentally cite
the wrong stream. See
[Provenance](concepts.md#provenance-spans-coverage-origins).

```{literalinclude} examples/06_write_decoder.py
:language: python
```

```console
$ python 06_write_decoder.py
file_kind: decode-stage
decoder:   http/1.1 tutorial
  dec:http-request: b'GET /users/1 HTTP/1.1\r\nHost: api.example'
  dec:http-response: b'HTTP/1.1 200 OK\r\nContent-Type: applicati'
check_coverage: []
```

The two request records became one `dec:http-request` record, and the
`content_type` labels say what each payload is. The final
`check_coverage: []` is the important part: it confirms every input byte is
accounted for — which is the subject of the last step.

## 3. Undecoded regions and the coverage guarantee

A decoder must never *silently* drop input. The **coverage guarantee** is
that within each input stream, every offset is either covered by a decoded
record's span or named by an **Undecoded** block — and never both.

{func}`zpf.decode_stage` closes that loop for you. On a clean exit it marks
every byte you left uncited as Undecoded — `skipped` for data the decoder
passed over, `tcp-gap` for a reassembly hole — so the guarantee holds by
construction. (`undecodable` is *not* used by auto-fill: it is the decoder's
own claim that it *tried and could not parse* a region, so it is reserved
for an explicit {meth}`~zpf.DecodeStage.undecoded` call.) Pass
`fill_undecoded=False` to opt out and mark everything yourself.

Here a stream ends in a half-sent request the toy parser can't finish. The
decoder simply doesn't cite the tail; auto-fill marks it, and
{func}`zpf.check_coverage` confirms the guarantee holds. With auto-fill off
and the tail still unmarked, the same check reports a `coverage-gap`.

```{literalinclude} examples/07_coverage.py
:language: python
```

```console
$ python 07_coverage.py
auto-fill on:  []
auto-filled reason='skipped' over [48, 77)
auto-fill off: coverage-gap: input stream (session 7, pid 0): [48, 77) is neither decoded nor marked Undecoded
```

The same check is available from the command line, which is how you'd
validate a decoder's output against its input in a pipeline:

```console
$ zpf validate rest_decoded.zpf --input rest_raw.zpf
rest_decoded.zpf: OK
```

## Where to go next

- [Concepts](concepts.md) — the normative model: [file
  kinds](concepts.md#file-kinds-raw-decode-stage-pass-through),
  [spans, coverage, and
  origins](concepts.md#provenance-spans-coverage-origins), and how chaining
  (`raw → tls-records → http`) is the same file-to-file mechanism applied
  twice.
- The [decode-stage how-to](howto/decode_stage.md) for the task-shaped
  recipe, and the [validate how-to](howto/validate.md) for reading coverage
  diagnostics.
- [Read payloads as typed values](howto/payload_content.md) — the other end of
  the `content_type=` labels this tutorial wrote: how a consumer turns
  `dec:http-request` back into a parsed value.
- The API reference for [`zpf.decode`](../api/decode.md) (the orchestrator),
  [`zpf.reassembly`](../api/reassembly.md) (stream views), and
  [`zpf.transform`](../api/transform.md) ({func}`~zpf.check_coverage`).

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

A decoder reads its input one **participant stream** at a time. Each stream
is a sequence of byte-run records whose boundaries came from reassembly, not
from the application — so a single HTTP message can span several records, or
share one with the next. To cite input bytes precisely, the decoder works in
**logical stream offsets**: 0-based positions in the reassembled stream,
where byte 0 is the stream's first application byte (`isn + 1` when the TCP
handshake was seen). A record's first byte sits at `seq_start - (isn + 1)`.

```{literalinclude} examples/05_rest_raw_input.py
:language: python
```

```console
$ python 05_rest_raw_input.py
participant 0 (10.0.0.1:51000):
  offset   0: b'GET /users/1 HTTP/1.1\r\nHost: api.example.com\r\n'
  offset  46: b'Accept: application/json\r\n\r\n'
participant 1 (93.184.216.34:80):
  offset   0: b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 30\r\n\r\n{"id":1,"name":"Ada Lovelace"}'
```

The request arrived as two records; the decoder will reassemble them into
one message. Those offsets — `0` and `46` — are the coordinate system every
span in the next step is written in.

## 2. Write the decode stage

The decoder reassembles each stream, splits it into HTTP messages, and
emits one record per message. Three things make a file a decode stage,
and the {class}`~zpf.conformance.ConformanceChecker` enforces them:

- `produced_by`/`produced_at` on {func}`zpf.create` — required the moment a
  file becomes derived;
- a `zpf-input` **source** citing the raw file, with a `digest` so a
  consumer can confirm the input hasn't changed;
- `decoder=` on each record — a record is *decoded* precisely because it
  names the decoder that produced it.

Each record also carries a **span**: the `[off_start, off_end)` range of the
input stream its bytes came from, in the logical offsets from step 1. The
span's `session_id`/`participant_id` name the stream in the *input's*
namespace, not the decoded file's. See
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
record's span or named by an **Undecoded** block — and never both. Whatever
the decoder couldn't parse (a gap, a truncated message, an unknown protocol)
it says so out loud, with a reason.

Here a stream ends in a half-sent request the toy parser can't finish. The
decoder marks that tail Undecoded; {func}`zpf.check_coverage` then confirms
the guarantee holds — and reports a `coverage-gap` if the marker is left
out.

```{literalinclude} examples/07_coverage.py
:language: python
```

```console
$ python 07_coverage.py
with the Undecoded marker: []
without it: coverage-gap: input stream (session 7, pid 0): [48, 77) is neither decoded nor marked Undecoded
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
- The [API reference](../api/transform.md) for
  {func}`~zpf.check_coverage` and the merge transform.

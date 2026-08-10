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

On close the stage also ends each output session declaring `input_extents` —
how long each input stream it drew on was. That is what lets a consumer check
the guarantee **from your output alone**, with {func}`~zpf.check_extents` and
without opening the input: a hole at the *end* of a stream is otherwise
invisible, because coverage that stops early looks exactly like a stream that
was that short.

### Saying your own output breaks

Coverage is about the *input*. When your **output** has a break — you emitted
one unit, lost the next, and emitted the one after — say so with
{meth}`~zpf.DecodeStage.discontinuity`:

```python
stage.record(stream, ts=..., payload=plaintext, cites=(0, 50))
stage.record(
    stream, ts=..., payload=more, cites=(139, 200),
    seam=zpf.Seam(reason="tls-record-lost"),   # these two do not join
)
```

The break is declared **on the record whose seam it is**, rather than as a
separate call you have to sequence correctly. That is deliberate: the duty is
*do these two units join?*, it rests on what your stage did with its input, and
most of it is not mechanically decidable — so the API asks once per record
instead of leaving a block to be remembered. Omitting `seam=` means they join,
which is the common case and the one the specification names: framing bytes, a
nonce and a tag left undecoded between two units withhold nothing, and the
content either side runs straight on.

It is the mirror of {meth}`~zpf.DecodeStage.undecoded`, and the pair are easy
to confuse: an Undecoded block says *there were bytes over there I did not
decode*; a Discontinuity says *something is missing here, in what I produced*.
It discharges **no** coverage obligation, so a stage that both failed to decode
an input region and broke its output owes one of each.

Give `width=` only when the gap can be counted. Omitting it means the extent is
unknowable — which is not the same as `width=0`, and is the common case: a lost
TLS record's plaintext length cannot be recovered from the ciphertext. An
absent width contributes 0 to the offset arithmetic, so the records either side
sit adjacent; what the block asserts is not a length but that they **do not
join**.

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

### Filtering or reordering a decoded file

Keeping only some decoded records — or changing their order — is **not** a
pass-through, however byte-preserving it looks. Stored order is what *defines* a
decoded stream's offsets, so dropping or moving a record rewrites them, and the
output cannot claim to have preserved what it just moved. Both are therefore
decode stages, and {func}`zpf.rewrite_decoded` writes either:

```python
zpf.rewrite_decoded(
    "decoded.zpf", "requests.zpf",
    keep=lambda record: record.content_type == "dec:request",
    produced_by="zpf-filter 1.0", produced_at=datetime.now(tz=UTC),
    transform_params_digest="sha256:…",     # what this stage was configured with
)
```

It carries the obligations that follow: every survivor cites the input range it
came from, every dropped range is marked `skipped` so the coverage guarantee
still holds over the whole input, and the input's decoders are **inherited**
rather than re-invented — `decoder_id` names a layer, not a stage, and a
filtered HTTP message is still an HTTP message. The stage identifies itself
through `produced_by`.

Pass `reorder=` to rearrange a participant's surviving records. Expect the
result to look wrong and not be: a reordering stage's `spans` will **not**
ascend with stored order, because the output's own offsets are recomputed from
the new order while the citations still point at where the bytes came from.
Coverage depends on which ranges are covered, not the order they appear in.

That gap is closed. Through `0.12` such a transform had no `params_digest` to
record its own configuration in, so the output stated *what* it came from but
not *how*; `0.14` adds `transform_params_digest` to the File Header for exactly
this case. Pass it to {func}`~zpf.rewrite_decoded` or {func}`~zpf.merge_files`.

```{admonition} The duty at a drop point
:class: note

A stage **MUST** emit a `Discontinuity` between two adjacent units of its own
output wherever those two do not join. {func}`~zpf.rewrite_decoded` does that
for you: at every drop point, and wherever a reorder separates records that
adjoined.

The two shapes are told apart by **width**. A *drop* withheld content of known
extent — the input range between the two survivors — so the block declares it,
with `reason="records-dropped"`. A *reorder* withheld nothing, and what lies
between two units that were never adjacent is not a hole to be counted, so the
block carries no width and contributes 0, with `reason="reordered"`.

This library emitted the block before the standard asked for it, behind a
`mark_gaps` switch, and reported the hole as
[zipline#78](https://github.com/adamkjonsson/zipline/issues/78). `0.15` closed
it, so the switch is gone: an always-conformant writer has no business
offering output that is not.
```

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
  [`zpf.transform`](../../api/transform.md) ({func}`~zpf.check_coverage`,
  {func}`~zpf.rewrite_decoded`).

# Write decode-stage files

A **decode stage** is a file derived from a raw one by a decoder: its records
are whole application messages (an HTTP request, a JSON response) instead of
the transport byte-runs the raw file holds. This is the recipe; the
[decoder tutorial](../tutorial-decoding.md) is the full worked walkthrough,
built on the same example.

## What makes a file a decode stage

Three things, all enforced by the
{class}`~zpf.conformance.ConformanceChecker`:

1. **Provenance on the header.** Pass `produced_by` (and optionally
   `produced_at`) to {func}`zpf.create` — required the moment a file becomes
   derived.
2. **A `zpf-input` source** citing the raw predecessor, ideally with a
   `digest` so a consumer can confirm the input hasn't changed.
3. **A `decoder=` on every record.** A record is *decoded* precisely because
   it names the decoder that produced it.

```python
import zpf

with zpf.create("rest_decoded.zpf", tick_hz=1_000_000,
                produced_by="http-decode 1.0") as writer:
    source = writer.add_source("zpf-input", uri="rest_raw.zpf",
                               digest="sha256:...")
    decoder = writer.add_decoder("http/1.1", version="tutorial")
    with writer.begin_session(proto="http", key=KEY) as session:
        client = session.participant("10.0.0.1:51000")
        session.record(
            client, ts=1000, payload=message_bytes, decoder=decoder,
            content_type="dec:http-request",
            spans=[zpf.Span(source_id=source.source_id, session_id=0,
                            participant_id=0, off_start=0, off_end=73)],
        )
```

## Cite input bytes with spans

Each decoded record carries a **span**: the `[off_start, off_end)` range of
the input stream its bytes came from, in **logical stream offsets** (0-based
positions in the reassembled stream, where byte 0 is the first application
byte). The span's `session_id`/`participant_id` name the stream in the
*input's* namespace, not the decoded file's. One message reassembled from
several input records still carries a single covering span. See
[Provenance](../concepts.md#provenance-spans-coverage-origins).

## Mark what you couldn't decode

A decoder must never *silently* drop input. Whatever it can't parse — a gap,
a truncated message, an unknown sub-protocol — it names with an
{class}`~zpf.Undecoded` block over the same logical offsets, with a reason:

```python
writer.undecoded(source, 0, 0, 48, 77, reason="incomplete request", decoder=decoder)
#                ^source ^sid ^pid ^off_start/off_end
```

## Check the coverage guarantee

The **coverage guarantee**: within each input stream, every offset is either
covered by a decoded record's span or named by an Undecoded block — never
both, never neither. {func}`zpf.check_coverage` verifies it, returning the
violations (empty when it holds):

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
- The [API reference](../../api/transform.md) for {func}`zpf.check_coverage`.

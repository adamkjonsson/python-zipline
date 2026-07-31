# Provenance

A derived `.zpf` — a decode stage or a pass-through merge — must record where
its bytes came from, so a consumer can **trust** it (this really was built from
that input), **verify** it (the input hasn't changed underneath), and **recover**
the original bytes behind anything it could not decode. This guide explains that
provenance system end to end. For the normative model see
[Concepts](../concepts.md#provenance-spans-coverage-origins); for authoring
spans while decoding, the [decoding guide](decoding.md).

## The input source and its digest

Every derived file declares each input it was built from as a source of kind
`zpf-input`, carrying the input's `uri` and a content `digest`. That digest is
the dependency edge: a consumer re-hashes the input and compares, confirming the
derived file still matches what it claims to describe.

You rarely write this by hand. {func}`zpf.decode_stage` and
{meth}`zpf.FileWriter.derive_from` declare the source and compute the digest from
the input for you; {meth}`~zpf.FileReader.digest` is the same hash a consumer
uses to check the match:

```python
with zpf.open("rest_raw.zpf") as raw:
    expected = raw.digest()                 # "sha256:..."
with zpf.open("rest_decoded.zpf") as decoded:
    (source,) = [s for s in decoded.sources.values() if s.kind is zpf.SourceKind.ZPF_INPUT]
    assert source.digest == expected        # the input is the one it was built from
```

## Two ways bytes are traced

How a derived record points back at its input depends on the kind of transform:

- A **decode stage** re-cuts the byte stream into application messages, so each
  record cites the input range it was built from as a **span set** — one or more
  {class}`~zpf.Span`s of the form `{source_id, session_id, pid, off_start,
  off_end}`. A message reassembled from several input records still carries a
  single covering span.
- A **pass-through** merge preserves each stream's bytes and boundaries, so
  there is nothing per-record to cite. Instead every participant carries a single
  {class}`~zpf.Origin` naming the input stream it re-emits, and offset
  preservation does the rest — the whole stream's provenance in one reference.
  ({func}`zpf.merge_files <zpf.transform.merge_files>` writes these.)

Both use the same coordinate system: **logical stream offsets** into the input's
participant streams, read in the *input's* id namespace (not the derived file's).
Reading spans back is just an attribute:

```python
for record in decoded.session(7).records():
    for span in record.spans:
        print(span.source_id, span.session_id, span.participant_id,
              span.off_start, span.off_end)
```

Authoring spans is covered in the [decoding guide](decoding.md#writing-the-decode-stage)
— `cites=` / {meth}`~zpf.reassembly.StreamView.cite` fill the ids in for you, so
a record can only cite the stream it came from.

## Coverage: nothing silently dropped

Spans say what *was* decoded; the **coverage guarantee** makes the negative space
explicit too. Within each input stream, every offset must be either cited by a
decoded record or named by an {class}`~zpf.Undecoded` block — never both, never
neither. An `Undecoded` block carries a `reason` that says whether the bytes are
recoverable:

- `undecodable` / `skipped` — the bytes **exist** at that range in the input
  (the decoder tried and failed, or simply passed over them); a consumer can
  follow the reference to fetch them.
- `gap` / `truncated` — the range is a **hole** with no bytes anywhere
  upstream.

The **class**, not the word, is what a consumer acts on — whether the bytes are
fetchable at all. The vocabulary is open so a producer can be more specific
about *how* a region came to be undecoded, and `reason_class` (`"bytes"` or
`"hole"`) is what stops that freedom costing the consumer its one actionable
fact. It is **required** with any reason outside the canonical four:

```python
writer.undecoded(source, 7, 0, 100, 139,
                 reason="rtp-seq-gap", reason_class="hole")
```

{attr}`Undecoded.recoverability <zpf.Undecoded.recoverability>` resolves the two
into that single fact, and returns `None` when a writer left it genuinely
unknowable. Do not guess there — least of all `"hole"`, which would silently
discard bytes that may well exist.

{func}`zpf.check_coverage` verifies the guarantee, returning the violations
(empty when it holds). The [validate how-to](../howto/validate.md#reading-the-diagnostics)
lists the `coverage-gap` / `coverage-overlap` / `coverage-excess` categories and
their fixes; a decode stage written with {func}`zpf.decode_stage` satisfies the
guarantee by construction (see [decoding](decoding.md#coverage-is-handled-for-you)).

## Following the chain

Because a derived file cites its input in the input's own coordinate system,
provenance **recurses**. `raw → tls-records → http` is two decode stages; a
consumer holding an HTTP record follows its span into the TLS-record stream, and
if that range is itself a derived file's output, follows *its* provenance one
level further — until it reaches the capture-sourced raw file that holds the
actual bytes. The `digest` at each hop confirms the link is still valid.

This is what makes an `undecodable` marker useful rather than a dead end: the
bytes are not in the decoded file, but the chain says exactly where they are. A
missing intermediate file is the only thing that stops recovery, and the digest
mismatch tells you which one.

{func}`zpf.resolve_spans` walks it for you. The walk is one hop or two,
depending on whether the file's own stage built the record:

```python
# A decode stage's record carries spans of its own: one hop.
spans = zpf.resolve_spans("decoded.zpf", session_id=7, pid=0, index=0)

# A pass-through's record carries none, so the walk goes through the
# participant's origin into the input and reads the spans it finds there.
spans = zpf.resolve_spans("annotated.zpf", session_id=7, pid=0, index=0)
```

The second case is the asymmetry worth knowing about: **an annotated file alone
cannot say which raw bytes a record came from.** Its records carry no `spans` —
that is exactly what marks them as re-emitted rather than built — so the answer
lives one file further down. Chained pass-throughs recurse. Inputs are resolved
beside the file by default; pass `open_input` when they live elsewhere.

## Each layer has its own offset space

One subtlety catches people resolving a *decoded* stream. A **transport**
stream's offsets are true positions, so a hole counts as if its bytes were
present. A **decoded** stream is a different object: its space is the
concatenation of that participant's record payloads in stored order, and
Undecoded regions name ranges in the *input's* space, so they contribute
nothing. It is not hole-inclusive.

A decoded record therefore carries no offset field at all — its place is
implied by the records before it. {meth}`SessionReader.ranges
<zpf.reader.SessionReader.ranges>` recovers it, one range per record, matching
{meth}`~zpf.reader.SessionReader.stream` positionally:

```python
for record, (start, end) in zip(session.stream(0), session.ranges(0), strict=True):
    print(start, end, record.content_type)
```

Resolving a single record costs O(k) on its face, so the whole participant's
table is built on first use and kept — forward reading pays nothing, and random
access is O(1) after.

## See also

- [Concepts: provenance](../concepts.md#provenance-spans-coverage-origins) — the
  normative model for sources, spans, coverage, and origins.
- [Validate a file](../howto/validate.md) — running the checks and reading the
  coverage diagnostics.
- [Decoding](decoding.md) — authoring spans and letting coverage auto-fill.
- API reference: [`zpf.transform`](../../api/transform.md)
  ({func}`~zpf.check_coverage`, {func}`~zpf.resolve_spans`,
  {func}`~zpf.merge_files`) and the
  {class}`~zpf.Span` / {class}`~zpf.Undecoded` / {class}`~zpf.Origin` blocks.

# Guides

Feature-oriented guides. Each takes one capability of `zpf` — reading files,
decoding, provenance, ordering, the two faces — and explains what it is, when
to reach for it, and how to use it well, including its advanced corners.

These sit between the [tutorials](../tutorial.md), which walk a single path
end to end, and the [API reference](../../api/index.md), which lists every
symbol:

- a **guide** explains a feature and how to wield it;
- [Concepts](../concepts.md) explains the *model* a feature rests on — guides
  link down into it rather than restate it;
- a [how-to](../howto/index.md) is the short recipe once you know the feature.

## Available guides

- **[Reading files](reading.md)** — opening files, sessions and participants,
  the three record iterators, and laziness.
- **[Decoding](decoding.md)** — reassembly views, stream vs datagram input,
  writing a decode stage, coverage, reading a payload as its `content_type`
  says, and (advanced) multiple decoders and decoder chaining.
- **[Provenance](provenance.md)** — input sources and digests, spans, origins,
  the coverage guarantee, and following the provenance chain to recover bytes.
- **[Ordering](ordering.md)** — causal order from `seq`/`ack`, `timeline()`,
  sequenced sessions and `verify()`, `SINGLE_CLOCK`, and merging captured
  directions.
- **[Faces and I/O](faces-and-io.md)** — binary vs JSON-Lines, lossless
  conversion, the flat block layer, and reading imperfect files.

## Advanced topics

Advanced material is **not** a bucket of its own: it lives under an
**Advanced** heading at the end of the guide it extends, so a feature's whole
story — basic to expert — stays in one place. This is the index into it:

- [More than one decoder in a session](decoding.md#more-than-one-decoder-in-a-session)
  — a session can mix decoders; pick one per record with `decoder=`.
- [Decoder chaining](decoding.md#decoder-chaining) — `raw → tls-records → http`
  as successive decode stages, and how provenance recurses through them.
- [Block pipelines](faces-and-io.md#block-pipelines) — filter or rewrite a file
  block by block, in either face, without going through the session API.
- [One block at a time](faces-and-io.md#one-block-at-a-time) — the
  single-block JSONL helpers, for tests and fixtures.

```{toctree}
:maxdepth: 1
:hidden:

reading
decoding
provenance
ordering
faces-and-io
```

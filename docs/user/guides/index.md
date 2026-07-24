# Guides

Feature-oriented guides. Each takes one capability of `zpf` — reading files,
decoding, provenance, ordering — and explains what it is, when to reach for
it, and how to use it well, including its advanced corners.

These sit between the [tutorials](../tutorial.md), which walk a single path
end to end, and the [API reference](../../api/index.md), which lists every
symbol:

- a **guide** explains a feature and how to wield it;
- [Concepts](../concepts.md) explains the *model* a feature rests on — guides
  link down into it rather than restate it;
- a [how-to](../howto/index.md) is the short recipe once you know the feature.

## Available guides

- **[Decoding](decoding.md)** — reassembly views, stream vs datagram input,
  writing a decode stage, coverage, and (advanced) multiple decoders and
  decoder chaining.
- **[Provenance](provenance.md)** — input sources and digests, spans, origins,
  the coverage guarantee, and following the provenance chain to recover bytes.
- **[Ordering](ordering.md)** — causal order from `seq`/`ack`, `timeline()`,
  sequenced sessions and `verify()`, `SINGLE_CLOCK`, and merging captured
  directions.

```{toctree}
:maxdepth: 1
:hidden:

decoding
provenance
ordering
```

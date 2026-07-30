# zpf — Zipline Payload Format

`zpf` is a Python implementation of version 0.9 of the
[Zipline Payload Format](https://github.com/adamkjonsson/zipline/blob/v0.9/docs/payload-format.md) (`.zpf`):
a file format for the payload of network traffic — the bytes exchanged
between endpoints once packets have been reassembled into sessions, plus
the metadata needed to consume them. Where a packet capture answers "what
was on the wire?", a `.zpf` file answers "what did the endpoints say to
each other, and in what order?"

```{note}
Version 0.9 was published as "1.0" and designated final, then retroactively
renumbered once the first implementation forced a breaking revision. The
current specification is
[0.10](https://github.com/adamkjonsson/zipline/blob/v0.10/docs/zipline-payload-format.md),
which this library does not yet implement; 0.10 claims no compatibility with
0.9. Everything below describes 0.9.
```

The library has zero runtime dependencies, is fully type-annotated, and
everything is re-exported at the package top level — `import zpf` is all a
consumer needs:

```python
import zpf

with zpf.open("cap.zpf") as f:
    for session in f.sessions():
        for record in session.timeline():   # causal order, clock skew or not
            print(record.timestamp, record.payload)
```

Where to go next:

- New to the format? Start with the [tutorial](user/tutorial.md), then read
  [Concepts](user/concepts.md) for the mental model behind it.
- Want a feature in depth? The [guides](user/guides/index.md) explain each
  capability and how to use it well.
- Have a task in mind? See the [how-to guides](user/howto/index.md) or the
  [`zpf` command-line tool](user/cli.md).
- Looking something up? The [API reference](api/index.md) covers every
  public module.
- Working on this library itself? The developer guide starts at
  [Architecture](dev/architecture.md).

```{toctree}
:maxdepth: 2
:caption: Getting started

user/installation
user/tutorial
user/tutorial-decoding
```

```{toctree}
:maxdepth: 2
:caption: Guides

user/guides/index
```

```{toctree}
:maxdepth: 2
:caption: Concepts

user/concepts
```

```{toctree}
:maxdepth: 2
:caption: How-to guides

user/howto/index
```

```{toctree}
:maxdepth: 2
:caption: Reference

api/index
user/cli
user/errors
```

```{toctree}
:maxdepth: 2
:caption: Developer guide

dev/architecture
dev/testing
dev/conformance
dev/contributing
```

# python-zipline

`zpf` — a Python implementation of the Zipline Payload Format.

The Zipline Payload Format (`.zpf`) stores the payload of network traffic —
the bytes exchanged between endpoints once packets have been reassembled into
sessions, plus the metadata needed to consume them. This library implements
[version 0.12](https://github.com/adamkjonsson/zipline/blob/v0.12/docs/zipline-payload-format.md)
of the format, specified in the
[zipline repository](https://github.com/adamkjonsson/zipline).

> **The format is in `0.x`, and every minor is a separate format.** A reader
> must reject a `version_minor` it does not implement, and no upgrade path
> between `0.x` versions is guaranteed. `zpf` therefore implements exactly one
> version — `zpf.SPEC_VERSION` tells you which — and a file written against an
> earlier one is rejected at the version gate rather than misread. Regenerate
> such a file from its capture rather than transcoding it.

The package is named `zpf` (not `zipline`) because the `zipline` name on PyPI
belongs to an unrelated project.

## Features

- Complete 0.12 support: the binary container and the JSON-Lines projection,
  raw, decode-stage, and pass-through files, with lossless converters.
- Verified against the specification's own 26 conformance vectors, vendored in
  [`tests/vectors/`](tests/vectors/) and run by the test suite.
- An ergonomic writer (`zpf.create`) that makes non-conformant files hard to
  write, and a session-first reader (`zpf.open`) with lazy record access.
- Causal ordering: `session.timeline()` orders records by TCP seq/ack
  happens-before, so directions captured with skewed clocks interleave
  correctly; the merge transform bakes that order into a sequenced file.
- Conformance checking against the specification's semantic rules, plus a
  decode-stage coverage validator.
- Zero runtime dependencies; fully type-annotated.

## Example

```python
import zpf

with zpf.create("out.zpf", tick_hz=1_000_000) as w:
    w.add_source("capture", uri="tap.pcap")
    with w.begin_session(proto="tcp", key="10.0.0.1:51000 <-> 93.184.216.34:80") as s:
        alice = s.participant("10.0.0.1:51000", isn=1000)
        s.record(alice, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n",
                 seq_start=1001, ack=5001)
        s.end(reason="fin")

with zpf.open("out.zpf") as f:
    for session in f.sessions():
        for record in session.records():
            print(record.timestamp, record.payload)
```

The `zpf` command-line tool inspects, converts, validates, and merges files:

```bash
zpf info cap.zpf
zpf cat cap.zpf                       # dump as JSONL
zpf convert cap.zpf cap.zpf.jsonl
zpf validate cap.zpf --verify
zpf merge sideA.zpf sideB.zpf -o merged.zpf
```

## Documentation

Full documentation lives in [`docs/`](docs/) and builds with Sphinx
(`cd docs && make html`, output in `docs/_build/html`). Start here:

- [Tutorial](docs/user/tutorial.md) — write and read your first file in four
  short stages, then [write a decoder](docs/user/tutorial-decoding.md).
- [Concepts](docs/user/concepts.md) — the format's mental model.
- [How-to guides](docs/user/howto/index.md) — task-shaped recipes
  (convert, validate, merge, decode-stage, robustness).
- [CLI reference](docs/user/cli.md) and [API reference](docs/api/index.md).
- [Developer guide](docs/dev/architecture.md) — architecture, testing,
  conformance strategy, and [contributing](CONTRIBUTING.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements.txt
.venv/bin/pytest                                  # run tests
ruff check .                                      # lint
.venv/bin/sphinx-build -W docs docs/_build/html   # build docs (warnings are errors)
.venv/bin/python -m build                         # build wheel + sdist
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor guide — code
style, docstring conventions, and the release checklist.

## License

MIT — see [LICENSE](LICENSE).

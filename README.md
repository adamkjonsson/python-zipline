# python-zipline

`zpf` — a Python implementation of the Zipline Payload Format.

The Zipline Payload Format (`.zpf`) stores the payload of network traffic —
the bytes exchanged between endpoints once packets have been reassembled into
sessions, plus the metadata needed to consume them. Version 1.0 of the format
is specified in the [zipline repository](https://github.com/adamkjonsson/zipline).

The package is named `zpf` (not `zipline`) because the `zipline` name on PyPI
belongs to an unrelated project.

## Features

- Complete v1.0 support: the binary container and the JSON-Lines projection,
  raw, decode-stage, and pass-through files, with lossless converters.
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

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements.txt
.venv/bin/pytest                                  # run tests
ruff check .                                      # lint
.venv/bin/sphinx-build docs docs/_build/html      # build docs
.venv/bin/python -m build                         # build wheel + sdist
```

## License

MIT — see [LICENSE](LICENSE).

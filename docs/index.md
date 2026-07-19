# zpf — Zipline Payload Format

A Python implementation of v1.0 of the
[Zipline Payload Format](https://github.com/adamkjonsson/zipline) (`.zpf`):
a file format for the payload of network traffic — the bytes exchanged
between endpoints once packets have been reassembled into sessions, plus the
metadata needed to consume them.

Everything is re-exported at the package top level; `import zpf` is all a
consumer needs.

## Quickstart

Writing a capture, reading it back in causal order, and using the CLI:

```python
import zpf

with zpf.create("out.zpf", tick_hz=1_000_000) as w:
    w.add_source("capture", uri="tap.pcap")
    with w.begin_session(proto="tcp") as s:
        alice = s.participant("10.0.0.1:51000", isn=1000)
        s.record(alice, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n",
                 seq_start=1001, ack=5001)
        s.end(reason="fin")

with zpf.open("out.zpf") as f:
    for session in f.sessions():
        for record in session.timeline():
            ...
```

```shell
zpf info cap.zpf
zpf cat cap.zpf
zpf validate cap.zpf --verify
zpf merge sideA.zpf sideB.zpf -o merged.zpf
```

```{toctree}
:maxdepth: 2
:caption: User guide

user/installation
user/tutorial
user/concepts
user/howto/index
user/cli
user/errors
```

```{toctree}
:maxdepth: 2
:caption: API reference

api/index
```

```{toctree}
:maxdepth: 2
:caption: Developer guide

dev/architecture
dev/testing
dev/conformance
dev/contributing
```

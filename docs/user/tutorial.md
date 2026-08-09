# Tutorial

This tutorial builds up `zpf`'s API in four short stages, each a complete,
runnable script under
[`docs/user/examples/`](https://github.com/adamkjonsson/python-zipline/tree/main/docs/user/examples).
Copy a script into a scratch directory and run it with plain `python` — no
project setup beyond `pip install zpf` (see [Installation](installation.md))
is needed. The stages build on one running example: a single HTTP
request/response between a client (`10.0.0.1:51000`) and a server
(`93.184.216.34:80`).

Each stage assumes only the ones before it. [Concepts](concepts.md) is the
companion reference — this tutorial links into it as new vocabulary shows
up, but explains just enough to keep moving.

## 1. Write your first file

`zpf.create` opens a file for writing. A file needs at least one **source**
(where the bytes came from) and, inside a **session** (one conversation),
the **participants** that took part. Every id — session, participant,
source — comes back as a handle, so later calls (like `record`) can't
reference something that was never declared: see
[Streamable by construction](concepts.md#streamable-by-construction-declare-on-first-use).

```{literalinclude} examples/01_write_first_file.py
:language: python
```

Timestamps are integers, not wall-clock times: `tick_hz=1_000_000` says
they're counted in microseconds. See
[Time: ticks on a capture clock](concepts.md#time-ticks-on-a-capture-clock)
for why capture time, not wall time, is what makes replay deterministic.
`seq_start`/`ack` are the raw TCP sequence and acknowledgement numbers off
the wire — optional, but they're what stage 3 uses to get ordering right.

Run it:

```console
$ python 01_write_first_file.py
wrote session.zpf (388 bytes)
```

## 2. Read it back

`zpf.open` runs one indexing pass and hands back a {class}`~zpf.FileReader`:
sessions, each with participants and records.

```{literalinclude} examples/02_read_it_back.py
:language: python
```

```console
$ python 02_read_it_back.py
face: binary, kind: raw, complete: True
session 0: tcp '10.0.0.1:51000 <-> 93.184.216.34:80'
  participant 0: 10.0.0.1:51000
  participant 1: 93.184.216.34:80
  ts=1000 10.0.0.1:51000 -> b'GET / HTTP/1.1\r\n\r\n'
  ts=1005 93.184.216.34:80 -> b'HTTP/1.1 200 OK\r\n\r\nhi'
```

`session.records()` reads each record from disk as the loop asks for it —
opening a file does not load its payloads into memory. `reader.stream_kind(7, 0)`
reports `(CAPTURE, TRANSPORT)` for this stream: its records reference a
`capture` source directly, and no decoder ran over them. The two answers are
independent — see
[File kinds](concepts.md#file-kinds-raw-decode-stage-pass-through).

## 3. Causal order

The previous file was captured at one vantage point, so its timestamps and
its causal order agree. That stops being true the moment a conversation's
two directions are captured at *different* tap points: their clocks can
drift apart, and sorting by timestamp can put an answer before the question
it answers. See
[Ordering: why timestamps are not enough](concepts.md#ordering-why-timestamps-are-not-enough)
for the full argument; here's what it looks like in code.

```{literalinclude} examples/03_causal_order.py
:language: python
```

```console
$ python 03_causal_order.py
sorted by timestamp: [b'HTT', b'GET']
session.timeline(): [b'GET', b'HTT']
merged session sequenced: True
merged order: [b'GET', b'HTT']
verify(): OK
```

Two things happened:

1. In one file with both participants, sorting by `timestamp` puts the
   response first — wrong. `session.timeline()` uses the `ack` instead: the
   response acknowledges the request's bytes, so it must come after,
   regardless of what either clock says. This is the same streaming merge
   {func}`zpf.causal_merge` implements, run for you.
2. The realistic version of the same problem — two *separate* files, one
   per tap point — is exactly what {func}`zpf.merge_files` (`zpf merge` on
   the command line) is for: it runs the merge once and writes a
   **SEQUENCED** file, so every later reader just replays stored order. The
   printed order after merging matches `timeline()`'s, and
   `session.verify()` confirms the stored order really is a valid causal
   linearization — see [Sequenced sessions](concepts.md#sequenced-sessions).
   The [merge how-to](howto/merge.md) covers this transform in full.

## 4. The two faces

Every `.zpf` file has two interchangeable encodings: the binary container
(what the last three stages wrote) and a JSON-Lines projection for
debugging and small captures. Conversion is lossless in both directions —
see [Two faces of one model](concepts.md#two-faces-of-one-model).

```{literalinclude} examples/04_two_faces.py
:language: python
```

```console
$ python 04_two_faces.py
9 lines; the Record line looks like:
{"type":"record","session_id":0,"sender_pid":0,"source_id":0,"ts":1000,"seq_start":1001,"ack":5001,"payload":"R0VUIC8gSFRUUC8xLjENCg0K"}
round trip: byte-identical
```

The `zpf` command line does the same conversions without writing any
Python:

```console
$ zpf convert session.zpf session.jsonl
$ cat session.jsonl        # inspect by eye
$ zpf convert session.jsonl session2.zpf
```

See the [CLI reference](cli.md) for every subcommand.

## Where to go next

- [Concepts](concepts.md) — the mental model this tutorial only sketched:
  file kinds, spans and provenance, the SEQUENCED flag, robustness to
  truncation and unknown blocks.
- [Tutorial: writing a decoder](tutorial-decoding.md) — the next step:
  turning a raw file into a decode stage of application messages.
- The [how-to guides](howto/index.md) — task-shaped recipes that assume
  this tutorial, including the [merge](howto/merge.md) and
  [convert](howto/convert.md) transforms in full.
- The [CLI reference](cli.md) and the [API reference](../api/index.md) for
  every subcommand and every public module.
- [Errors and diagnostics](errors.md) for what happens when a file isn't
  clean.

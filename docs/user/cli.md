# CLI reference

Installing `zpf` puts a `zpf` command on your `PATH`. It wraps the same
library calls the [tutorial](tutorial.md) uses, so anything the command line
does you can also do in Python — the command is the convenient front door for
inspecting, converting, validating, and merging files without writing a
script.

```console
$ zpf --help
usage: zpf [-h] [--version] COMMAND ...

Inspect, convert, validate, and merge .zpf files.
```

Every subcommand takes a file in **either face** — the binary container or
the JSON-Lines projection — and detects which by content, so you never pass a
format flag just to be read. See [Two faces of one
model](concepts.md#two-faces-of-one-model).

## Exit codes

Uniform across subcommands:

| Code | Meaning |
| ---- | ------- |
| `0`  | Success, or a clean file with no findings. |
| `1`  | `validate` only — the file was read but has findings. |
| `2`  | Error: an unreadable or missing file, a structural violation, or bad usage. |

`1` is reserved for "the file is readable but not clean," so a script can tell
a nonconformant file (`1`) from one it couldn't open at all (`2`).

## `zpf info FILE`

Summarize a file: its face and [kind](concepts.md#file-kinds-raw-decode-stage-pass-through),
whether it is complete, the capture clock, header provenance, and one line
per source, decoder, and session. Wraps {func}`zpf.open`.

```console
$ zpf info session.zpf
face:      binary
kind:      raw
complete:  True
clock:     1000000 ticks/s
source 0: capture capture.pcap
session 0: proto=tcp key='10.0.0.1:51000 <-> 93.184.216.34:80' participants=[10.0.0.1:51000, 93.184.216.34:80] records=2 end=yes
```

A truncated file reports `complete:  False  (truncated)` and prints any
reader diagnostics as `note:` lines on **stderr**, so the summary on stdout
stays parseable:

```console
$ zpf info truncated.zpf
note: truncated: stream ends inside a block's content
face:      binary
kind:      raw
complete:  False  (truncated)
...
```

`info` never fails on a merely nonconformant or truncated file — it reports
what it found and exits `0`. Use `validate` when you want findings to drive
the exit code.

## `zpf cat FILE`

Dump every block as JSON-Lines on stdout — one JSON object per line, in file
order. This is the file's other face printed for the eye or for `grep`/`jq`;
it is exactly what `convert ... --to jsonl` writes.

```console
$ zpf cat session.zpf
{"type":"file","format":"zipline-payload/0.14","tick_hz":1000000}
{"type":"source","source_id":0,"kind":"capture","uri":"capture.pcap"}
{"type":"session","session_id":0,"proto":"tcp","key":"10.0.0.1:51000 <-> 93.184.216.34:80"}
{"type":"participant","session_id":0,"pid":0,"endpoint":["10.0.0.1:51000"],"isn":1000}
{"type":"participant","session_id":0,"pid":1,"endpoint":["93.184.216.34:80"],"isn":5000}
{"type":"record","session_id":0,"sender_pid":0,"source_id":0,"ts":1000,"seq_start":1001,"ack":5001,"payload":"R0VUIC8gSFRUUC8xLjENCg0K"}
{"type":"record","session_id":0,"sender_pid":1,"source_id":0,"ts":1005,"seq_start":5001,"ack":1019,"payload":"SFRUUC8xLjEgMjAwIE9LDQoNCmhp"}
{"type":"session_end","session_id":0}
{"type":"end"}
```

Payloads are base64 in the JSONL face. To recover the bytes, decode the
`payload` field — or read the file in Python, where `record.payload` is
already `bytes`.

## `zpf convert IN OUT`

```text
zpf convert IN OUT [--to {binary,jsonl}]
```

Convert a file to the other face, block by block, losslessly. With no
`--to`, the target is the opposite of the input's face (binary → jsonl,
jsonl → binary); pass `--to` to normalize a file to a named face regardless
of what it already is. Wraps the streaming block-copy pipeline.

```console
$ zpf convert session.zpf session.jsonl        # binary -> jsonl
$ zpf convert session.jsonl session2.zpf       # jsonl -> binary
```

The conversion is semantically lossless, and for a file `zpf` wrote the
round trip is **byte-identical**: `session2.zpf` equals `session.zpf` byte
for byte, unknown blocks and all. See the [convert how-to](howto/convert.md)
for the exact guarantee and its one caveat.

## `zpf validate FILE`

```text
zpf validate FILE [--strict] [--verify] [--input RAW]
```

Read a file and report conformance findings. Each finding prints as
`FILE: category: message` on stdout; a summary count goes to stderr. Exit
`0` when clean, `1` when there are findings, `2` when the file can't be read
at all.

```console
$ zpf validate session.zpf
session.zpf: OK
$ zpf validate truncated.zpf
truncated.zpf: 1 finding(s)
truncated.zpf: truncated: stream ends inside a block's content
```

| Flag | Effect |
| ---- | ------ |
| `--strict` | Escalate the first semantic violation or truncation to a hard error (exit `2`) instead of collecting it as a finding. |
| `--verify` | Additionally re-check every [SEQUENCED](concepts.md#sequenced-sessions) session's stored order against its ordering hints. |
| `--input RAW` | Additionally check the [decode-stage coverage guarantee](howto/decode_stage.md) against the cited input file `RAW`; wraps {func}`zpf.check_coverage`. |

```console
$ zpf validate rest_decoded.zpf --input rest_raw.zpf
rest_decoded.zpf: OK
```

See the [validate how-to](howto/validate.md) for what each category means
and how to read the diagnostics.

## `zpf merge SIDE_A SIDE_B -o OUT`

```text
zpf merge SIDE_A SIDE_B -o OUT [--produced-by TOOL]
```

Merge two separately-captured directions of one conversation into a single
**sequenced pass-through** file. Wraps {func}`zpf.merge_files`.

```console
$ zpf merge sideA.zpf sideB.zpf -o merged.zpf
$ zpf info merged.zpf
face:      binary
kind:      pass-through
...
source 0: zpf-input sideA.zpf
source 1: zpf-input sideB.zpf
session 0: proto=tcp key='...' participants=[...] records=2 sequenced end=yes
```

| Flag | Effect |
| ---- | ------ |
| `-o`, `--output` | Required. Where to write the merged binary file. |
| `--produced-by` | Transform provenance written to the output header (default: `zpf <version>`). |

Each input must be a raw capture holding exactly one session with one
participant (one captured direction), and the two must share a clock. The
[merge how-to](howto/merge.md) covers the preconditions and the result in
full.

## `zpf --version`

```console
$ zpf --version
zpf 0.1.0
```

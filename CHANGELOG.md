# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).

## Versioning

This library follows the same numbering rule as the format it implements,
on its own counter: while we are in `0.x`, **every minor is a break**, and
no upgrade path between two minors is promised. Patch releases are
backwards-compatible fixes against the same API and the same spec version.

The library version and the spec version are separate numbers and are not
expected to match. `zpf.SPEC_VERSION` states which version of the Zipline
Payload Format a release implements, and each entry below names it too. A
release that ports to a new spec minor is always a library minor bump,
because the format itself guarantees no upgrade path across one.

The version is declared once, in `pyproject.toml`; `zpf.__version__` reads
it back from the installed distribution metadata.

## [Unreleased] — 0.2.0, in development

Everything below is the **0.2.0** work. It is not released: `pyproject.toml`
declares `0.2.0.dev0`, and `0.1.0` remains the only tagged version. The
section becomes `## [0.2.0] - <date>` when it ships.

Implements spec **v0.16** (`SPEC_VERSION == (0, 16)`).

A break in both directions. Files written by 0.1.0 are stamped for spec 0.9
and are refused at the version gate, and files written by 0.2.0 will be
unreadable by 0.1.0. The reading and writing APIs moved with it: what 0.1.0
answered per *file*, 0.2.0 answers per *stream*, because the specification
made that the only unit at which the questions have an answer.

### Added

- `zpf.as_datetime(value, header)`: read a stored tick value as a
  timezone-aware UTC `datetime` — the read-side counterpart of
  `unix_seconds`, which only ever went the other way. A record cannot
  convert itself, since `tick_hz` and `time_epoch` live on the File Header,
  so this takes both. Keyed on the value rather than the record, so it reads
  `ts_first` as readily as `timestamp`, and `None` passes through for an
  absent optional. Integer arithmetic throughout: a nanosecond `tick_hz`
  lands on the correct microsecond, where one float division does not.
  Display and reporting only — timestamps are not an ordering key.
  ([#53](https://github.com/adamkjonsson/python-zipline/issues/53))

- `comment=` on `SessionWriter.record()` and `DecodeStage.record()`.
  `Record.comment` already encoded and read back, so the only way to emit
  one was `write_block` and hand-managed ids. It stays **free text**: a
  stage emitting one record per protocol field may use it to say which
  field a record is, but that is a stopgap for a name the format does not
  yet have ([#58](https://github.com/adamkjonsson/python-zipline/issues/58)).
  `extra_options` is now the only Record option without a keyword.
  ([#55](https://github.com/adamkjonsson/python-zipline/issues/55))

- `ts_first=` on `SessionWriter.record()`, so a record can say when it
  *started* as well as when it finished without dropping to
  `FileWriter.write_block` and hand-managing ids. The field and both faces
  already existed; only the ergonomic writer omitted it. This is the only
  way a **capture**-sourced file can record a coalesced record's start time,
  since spans must reference a `zpf-input` Source and a capture converter
  has none. ([#47](https://github.com/adamkjonsson/python-zipline/issues/47))

- A **write-time causal-order guard** on sequenced sessions. Asserting
  `sequenced=True` is a promise about records not yet written, and it is now
  checked as they arrive: the per-participant `seq_start` rule and, for a
  two-participant session, that no record follows a peer record which
  already acknowledged its bytes. On by default; `verify_order=False` on
  `begin_session` switches it off for deliberately writing a non-conformant
  file. The rule now has one implementation, driven reader-side by
  `verify_sequenced` and writer-side by `SessionWriter`.
  ([#49](https://github.com/adamkjonsson/python-zipline/issues/49))

- `linearize=True` on `begin_session`: buffer a session's records and emit
  them in causal order when it ends, so a producer supplies each direction
  in its own order — what stream reassembly gives it — and `causal_merge`
  computes the interleaving. Memory is unbounded for the session's lifetime,
  and `discontinuity()` is refused while it is on, a positional block being
  incompatible with reordering.
  ([#49](https://github.com/adamkjonsson/python-zipline/issues/49))

- `sequenced=True` on `decode_stage` and `FileWriter.derive_from`: a decode
  stage emits its records in the input's own timeline instead of stream by
  stream, and marks each session SEQUENCED. A stage decodes one stream at a
  time, so its natural output is two monologues rather than the conversation
  the input recorded; a decoded record's timestamp is the completion time of
  the last input record it came from, so ordering by it reproduces that
  timeline. Derived records are hint-less, so `sequenced_basis` is required
  — and it is **derived from the input** rather than guessed: `trivial` for
  a single-participant session, `protocol` where the input's records carried
  TCP hints, the input's own basis where it declared one, `clock` where the
  input declares SINGLE_CLOCK. An input supporting none of those raises
  rather than naming a basis that is not true.
  ([#50](https://github.com/adamkjonsson/python-zipline/issues/50))

### Added — the 0.16 port

- `zpf.content`: the `content_type` label grammar and the `prim:` decode —
  `ContentType`, `ContentRegistry`, `decode_prim`, `PRIM_WIDTHS`,
  `ContentError`, `Record.content()`, and `FileReader.content()`.
- A decoder-facing API for reading a stream as a decoder needs it:
  reassembly views (`StreamView`, `Segment`, `Gap`, `Datagram`), span
  helpers (`resolve_spans`, `record_ranges`, `stream_extent`), and the
  decode-stage orchestrator (`decode_stage`, `DecodeStage`, `DecodeStream`,
  `DerivedInput`, `Hints`) whose coverage guarantee holds by construction.
- `rewrite_decoded`: a decoded-layer filter and reordering stage.
- The 0.13/0.14 syntax — `Discontinuity`, `InputExtent`, `Break`,
  `check_extents`, the `sequenced_basis` and `reason_class` options, and the
  `SEQUENCED_BASES`, `REASON_CLASSES` and `UNDECODED_REASONS` vocabularies.
- `check_splice`: the splice duty, which is only visible across a *pair* of
  files and so cannot be checked one file at a time.
- `OutputLayer` and `stream_layer`: a decoder declares the layer it outputs,
  and an unrecognised value is reported as a plain `int`, never guessed.
- `Seam` — a break declared on the record whose seam it is, via
  `dec.record(..., seam=...)`.
- `AdvisoryError` and an advisory conformance tier, for the format's first
  violation class that must be *accepted* and reported rather than rejected.
- `unix_seconds`, and `datetime` accepted directly for `produced_at`.
- The conformance harness over the vendored spec vectors, with a
  `KNOWN_PASSING` ratchet that no name is ever removed from.
- A Sphinx/MyST documentation site, a CI workflow, and this changelog.

### Changed

- **The implemented spec version moved from 0.9 to 0.16**, through 0.12 and
  0.14. Every minor of the format is a separate format, so this is three
  breaks, not an upgrade.
- The Decoder body split from `<HH>` to `<HBB>` to carry `output_layer`;
  `add_decoder()` gained an `output_layer` parameter. The default is
  `DECODED`, numbered 0, so a decoder that names no layer encodes the same
  four bytes it always did.
- `SessionReader.is_decoded_stream(pid)` became `SessionReader.layer(pid)`,
  and the module-level equivalent became `stream_layer(records, decoders)` —
  resolving a layer needs the Decoder table, which a record sequence does not
  carry. It raises rather than guessing when records mix layers or name an
  undeclared `decoder_id`.
- `FileReader.file_kind()` became `FileReader.stream_kind(session_id, pid)`,
  returning `(provenance, layer)`. There is no file-wide answer, so `zpf
  info` now prints a line per stream.
- `record_ranges` and `stream_extent` take the layer as a **required**
  argument — no default, because a wrong one misplaces every record silently.
- `CoverageLedger.findings()` no longer takes a `file_kind` argument; the
  interior-gap check is now keyed per input stream.
- The package is classified `Development Status :: 3 - Alpha`, and the README
  warns that neither the format nor this library is production-ready.
- The version is declared in `pyproject.toml` instead of in
  `src/zpf/__init__.py`.

### Removed

- `mark_gaps`. 0.15 made the origination duty a MUST, so the switch could
  only ever produce non-conformant output.
- The file-kind machinery, in full: file-level purity locking, the
  raw / decode-stage / pass-through constants, and the rule that inferred a
  stream's provenance from its layer. Both steps of that inference were wrong.

### Fixed

- Documentation: nothing said that a **decoded file is always
  packet-oriented** as the next stage's input, so a decoder written against
  the tutorial worked at stage 1 and raised at stage 2 — a failure that only
  appears once stages are chained, and looks like a caller bug. The decoding
  tutorial now has a chaining section, and `DecodeStage.streams()` says to
  dispatch on `is_stream_oriented`. The behaviour is unchanged and correct; a
  decoded record is a self-contained unit. Note the exception: a
  sessionization stage emits a transport layer whose records carry `Hints`,
  so *its* output stays stream-oriented.
  ([#56](https://github.com/adamkjonsson/python-zipline/issues/56))
- Passing an open `FileReader` where a path or stream was expected — to
  `check_coverage`, `zpf.open`, or anything else routing through the same
  plumbing — raised an internal `AttributeError` about `seekable()`, which
  read as "your file object is wrong" rather than "this does not take
  readers". It now raises a `TypeError` naming `FileReader` and saying what
  to pass instead. The mistake is natural because `decode_stage` *does*
  accept a reader; the message says so.
  ([#57](https://github.com/adamkjonsson/python-zipline/issues/57))
- Producers could assert `SEQUENCED`, emit a badly interleaved session, and
  get a silently non-conformant file: the cross-participant ack rule was
  checked nowhere on the write path, and the per-participant rule the
  `ConformanceChecker` enforces cannot see an interleaving violation. Such a
  file was then trusted and mis-ordered by readers, which skip the merge for
  a sequenced session. See the guard above.
  ([#49](https://github.com/adamkjonsson/python-zipline/issues/49))
- The packaged `Specification` URL pointed at spec `v0.12` while the library
  implemented `0.16` — following it from the package page gave the wrong
  format, with nothing to signal it was wrong. Now pinned to `v0.16`, and
  held there by a test that compares it against `zpf.SPEC_VERSION`. A
  spec-bump checklist in `docs/dev/contributing.md` covers the places a
  version literal hides that no test can reach.
  ([#48](https://github.com/adamkjonsson/python-zipline/issues/48))
- Reserved bits set in record flags are ignored rather than isolating the
  block, and the writer generates only the flag bits the format names.
- A record whose `content_type` label is unusable is kept rather than
  dropped.

## [0.1.0] - 2026-07-10

The first release. Implements spec **v0.9** — which stamps
`version_major = 1`, `version_minor = 0`, because that version was published
as "1.0" and renumbered without rewriting its bytes.

### Added

- The typed block model and the binary container reader/writer.
- The JSON-Lines projection with lossless converters in both directions.
- Conformance checking and the ergonomic writer API.
- The session-first reader, `zpf.open`.
- The streaming causal merge and `SEQUENCED` verification.
- The merge transform, the coverage validator, and the `zpf` CLI.

[Unreleased]: https://github.com/adamkjonsson/python-zipline/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/adamkjonsson/python-zipline/releases/tag/v0.1.0

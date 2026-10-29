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

## [Unreleased]

### Changed

**Implements spec v0.24** (`SPEC_VERSION == (0, 24)`), the sessionizer
release ([#108](https://github.com/adamkjonsson/python-zipline/issues/108)).
Under the `0.x` rule every minor is a separate format, so files written by
`0.7.0` (stamped `0.23`) are no longer readable, and `0.7.0` refuses files this
release writes. The conformance vectors are re-vendored at `v0.24`: 77 entries,
90 files, nine of them new. A multi-file entry now declares its merged orders
per member file, and the harness checks them. The port is in progress; its
normative changes land under #104–#107.

**A transport stream's extent counts sequence numbers, and a FIN's position is
owed nothing** ([#104](https://github.com/adamkjonsson/python-zipline/issues/104),
[zipline#168](https://github.com/adamkjonsson/zipline/issues/168)). This changes
bytes a stage writes. A zero-length record now reaches its own offset, so on a
closed TCP stream whose final pure ACK is stored one past its FIN,
`StreamView.extent_and_gaps` measures one more than `0.7.0` did, and a
decode or sessionize stage declares that extent on its Session End. Through
`0.7.0` the stage declared one short while `check_coverage` measured the
spec's number, so the stage's own output failed its own check (`[4, 5)` on
`closed-final-ack`). The FIN's position between is not a hole and not a
byte: `check_coverage` exempts it, and auto-fill marks neither `gap` nor
`skipped` there.

- A pure ACK stored after a **lost tail** now shows it: the range up to the
  ACK is a hole, yielded by `chunks()` as a trailing `Gap` and filled `gap`.
  A FIN whose segment was lost leaves the same one-wide hole.
- **The single-file extent check tolerates exactly one position.** A FIN is
  found only in the input, so from a derived file alone a one-wide uncovered
  tail may be its position or one dropped byte. `extent-exceeds-coverage`
  (reader, writer, `check_extents`) fires on a shortfall of more than one,
  and `check_coverage`, which holds the input, decides the last one.
- `stream_extent` was already right, and is unchanged. Its docstring now
  states the definition, and that **reach is not a record's end**: a
  zero-length FIN ends one past its offset for the merge, but reaches only its
  offset.

### Added

**`zpf.fin_positions()` and `StreamView.fin_positions()`**, the positions
inside a stream's extent that a FIN occupies, which the coverage guarantee owes
nothing. The function works from a participant's blocks, as `stream_extent`
and `record_ranges` do. The method comes from the same walk that measures
`extent_and_gaps`, so a stage pays nothing more for it.

**`RecordFlags.COPY` (`0x0100`) and its JSONL token `copy`**, the Record flag
`0.24` assigns to a capture point's copy of a datagram
([zipline#169](https://github.com/adamkjonsson/zipline/issues/169)). Until
this release the bit was reserved and rendered as the hex token `"0x0100"`.
The reserved mask is now `0xFE20`. The bit round-trips. Its two writer rules
and the reader's check land with
[#106](https://github.com/adamkjonsson/python-zipline/issues/106).

## [0.7.0] - 2026-10-23

Implements spec **v0.23** (`SPEC_VERSION == (0, 23)`), unchanged from `0.6.0`.
**The first minor in four that is not a spec port**, and the one difference
that follows from it: the file format does not move, so files written by
`0.6.0` and by this release are read by both — verified in both directions,
including a `ts_first` this release writes where `0.6.0` would not have. What
makes it a minor is the API and the bytes a decode stage emits, not the gate.

Its subject is **streamability and cost**. The format's goals promise a
writer can flush a finished session and forget it, keeping memory bounded on
unbounded input; three of the five issues were places this library did not
deliver that, and they were found by measuring rather than by reading.
Recorded in `plans/V0.7.0-RELEASE-PLAN.md`.

| | `0.6.0` | `0.7.0` |
| --- | --- | --- |
| Write a derived file, 100k records | 13.7 MB | **0.2 MB** |
| `zpf.open()` a derived file, 100k records | 20.1 MB | **4.9 MB** |
| One unit of a 20k-record unit sequence | 22.4 MB | **1.1 MB** |
| A decode stage's parse passes over its input | 5 | **4** |
| `zpf.open()` with `check=False` | — | **30% faster** |

### Added

**`zpf.open(..., check=False)` declines the conformance checker**
([#97](https://github.com/adamkjonsson/python-zipline/issues/97)). Every read
ran the checker over every block, which is **~30% of the indexing pass**
(measured: 0.465 s → 0.328 s over 60,000 records), with no way to opt out.
It stays on by default and should, for any file you did not produce — it is
what populates `diagnostics` and `unplaceable`, and what `strict=`
escalates. `check=False` is for a file whose conformance you already know:
one the same process just wrote, a stage reading its own predecessor's
output inside one run, or a caller about to drive `zpf.ConformanceChecker`
itself.

What is given up is everything the checker finds — no `"nonconformant"`
diagnostics, no advisory findings, an empty `unplaceable`, because nothing
looked rather than because the file was clean. What is kept is the
structural tier, which belongs to the flat reader: truncation, trailing
bytes and a malformed frame raise or report exactly as before. The
self-derivation check is kept too, being a path comparison over the Source
table rather than part of the checker. `strict=True` with `check=False`
raises `ValueError`, since strict mode's whole content is to raise on what
the checker found.

### Changed

**A decode stage derives `ts_first`, as a sessionization stage already did**
([#87](https://github.com/adamkjonsson/python-zipline/issues/87)).
`DecodeStage.record` wrote `ts_first` only when given one; it now works it
out alongside `ts` from what a record cites — the earliest arrival among the
cited input records, each counting its own `ts_first` where it has one — and
writes it only where it is earlier than `ts`. So a unit built from one input
record still carries none, and a unit reassembled from several now carries
the time it started.

**This changes the bytes an existing `decode_stage` caller writes**, which is
why it waited: the option appears on every decoded unit drawn from more than
one input record, which is the ordinary case — a message spread over several
TCP records, a TLS record split across segments. `0.6.0` derived it in
`FlowSession.record` only, because `sessionize_stage` was new there and
deriving it could not change anything anyone had written, while doing it in
both would have moved output a downstream had just reviewed
([#86](https://github.com/adamkjonsson/python-zipline/issues/86)). That
release has shipped, and the two stages answer the specification's timestamp
rule the same way again.

An explicit `ts_first=` is still written as given. An explicit `ts=` still
leaves `ts_first` to the caller, in both stages, because deriving one half of
the pair against the caller's other half could place the first arrival after
the last.

**`zpf.stream_layer` takes an iterator**
([#93](https://github.com/adamkjonsson/python-zipline/issues/93)). Its
`records` parameter widens from `Sequence[Record]` to `Iterable[Record]`. It
reads each record's `decoder_id` and keeps nothing, so an iterator was always
enough; a caller passing a list is unaffected.

### Fixed

**A block naming an undeclared session no longer crashes the indexing pass.**
Found while building the above. `_file_into_index` looked its session up
directly, relying on the checker having already isolated such a block — so
with the checker declined, a record naming a session the file never declared
raised `KeyError` out of `zpf.open`. It is now dropped from the index
instead, which is what isolation did: a reader that cannot resolve the
reference has nothing to file the block against, and inventing the session
would be inventing structure the file does not have. Unreachable with
`check=True`, so no checked read changes.

**`SessionReader.layer()` no longer holds a whole participant in memory**
([#93](https://github.com/adamkjonsson/python-zipline/issues/93)). It
materialised every record of a participant to resolve one answer, which undid
the laziness `StreamView` promises for anything reaching it through
`is_unit_sequence` — so the *first* read of a `units` stream held the whole
stream before yielding its first record. It streams the records now. Taking a
single unit from a 20,000-record unit sequence: **22.4 MB → 1.1 MB**.

**A stage no longer re-reads its input to measure it**
([#93](https://github.com/adamkjonsson/python-zipline/issues/93)). The extent
a stage declares on each Session End, and the holes that decide which
auto-filled regions are `gap` rather than `skipped`, were measured by a fresh
walk of every input stream at the first Session End — a second full read of
data the stage's own caller had just read. Any complete walk of a
`StreamView` now records both numbers as it goes, and the new
`StreamView.extent_and_gaps()` reuses them; a view nothing walked is still
walked on demand, so the answer is the same either way. Measured on a
20,000-record unit-sequence input, a decode stage's parse calls fall from
five passes over the input to four.

The numbers are unchanged, including the two places it would be easy to
change them: a Discontinuity *after* the last record does not extend the
extent (as `zpf.stream_extent` already declined to count one), and an
abandoned walk records nothing, so a partial read cannot leave a partial
measurement behind.

**A derived file's write is bounded-memory again**
([#96](https://github.com/adamkjonsson/python-zipline/issues/96)). The
coverage ledger — which both the writer and the reader run, to check that
every offset of an input stream is either cited or marked — appended one
interval per span and merged none of them, so writing or reading a derived
file grew with its record count. The format's goals promise the opposite:
a writer *flushes a finished session and forgets it, keeping memory bounded
on unbounded input*. Session state was already dropped at Session End; this
sat behind it.

Both ledgers now hold their ranges coalesced — sorted, disjoint, touching
ranges fused — maintained as each range arrives. For the ordinary case of a
stage citing its input in order, that is **one range per input stream**
however many records cite it. Measured, 100,000 records with a 64-byte
payload:

| | `0.6.0` | now |
| --- | --- | --- |
| Write a derived file | 13.7 MB | **0.2 MB** |
| `zpf.open()` a derived file | 20.1 MB | **4.9 MB** |

The read-side figure is now the same as for a capture-sourced file of the
same size: what remains is the reader's structural index, which is eager by
design and is what buys independent per-session iterators and O(1) random
access.

No verdict changes — the ranges were only ever consumed merged, and nothing
asks either ledger *which* range covered an offset. One error message can
now name a wider range than before: where auto-fill reports a range both
cited and explicitly marked, coalesced inputs report the whole clash where
unmerged ones reported its first fragment. The raise, and the ranges it
covers, are unchanged.

Two notes for anyone reading the issue alongside this entry. The flag it
proposed for *did any record cite this stream* proved unnecessary:
coalescing never empties a non-empty set, so the existing emptiness test
still answers the question. And the saving is per **input stream**, which is
where the guarantee is scoped — a file deriving 10,000 input streams still
holds a ledger each.

### Documentation

**The reading guide no longer promises flat memory**
([#98](https://github.com/adamkjonsson/python-zipline/issues/98)). Its
*Laziness* section said *"nothing buffers the whole file, so memory stays
flat regardless of size"*. Three parts of that were wrong: the JSON-Lines
face holds every block, because a text stream cannot be seeked back to one;
the binary index keeps a locator per record; and a `_SessionIndex` per
session costs considerably more than a record does. The page now states what
the reader is — lazy in payloads, eager in structure — with the measured
costs (~50 bytes per record, ~1.9 kB per session, nothing per byte of
payload; ~384 bytes per record on the JSONL face), and points at
`BlockReader`/`JsonlReader` as the bounded-memory path for input whose size
you do not control.

`concepts.md`'s *Streamable by construction* is a statement about the
**format** and stays as it was, with one addition: this library's writer
takes the flush-and-forget option and its reader deliberately does not, so
the two pages cannot be read as one promise.

## [0.6.0] - 2026-10-18

Implements spec **v0.23** (`SPEC_VERSION == (0, 23)`), up from `0.21`. Files
written by `0.5.x` are refused at the version gate, and files written by this
release are unreadable by `0.5.x` — the `0.x` rule, as before. No release of
this library implements `0.22`: `0.23` followed it within days, to fix the
pure-ACK edge this port found, and this release takes both.

### Changed

**Acks are positions, so a sequenced stream past 2 GiB orders and verifies**
([#75](https://github.com/adamkjonsson/python-zipline/issues/75),
[zipline#151](https://github.com/adamkjonsson/zipline/issues/151)). The causal
merge and the stored-order check compared a record's `ack` with peer record
ends by serial arithmetic, which is only defined within 2³¹. A peer whose last
stored ack is far behind — a server that sends its SYN-ACK and then only pure
ACKs, which are not records — read every client record 2 GiB past it as
*below* the ack. So `verify()` and the writer's linearize guard refused a
correct upload at its 2 GiB record, and the merge could hold a record back
past where it belongs. Since `0.22` an ack is unwrapped along its sender's
records into the peer's offset space, the way offsets have been since `0.21`,
and compared with record ends as a plain integer:

- Positions are **signed**: in a mid-stream capture an ack can lie below the
  peer's first captured byte, and acknowledges no captured byte.
- An **unplaceable** record has no end, so no edge runs from it; its own ack
  still orders it.
- In the merge, a record whose peer's next record is unplaceable now waits for
  the placeable records behind it, where it used to overtake them.
- Every file that verified before still verifies; every merge under 2 GiB is
  unchanged except for that last case.

`verify_sequenced` and `causal_merge` take a new keyword, `isns`: each
participant's declared `isn`, which anchors its offsets at `isn + 1`. A
participant missing from it is anchored at its first captured byte.
`SessionReader.verify()`, `SessionReader.timeline()`, the writer and
`merge_files` pass their participants' values.

**`decode_stage` refuses `output_layer` alongside a `DecoderHandle`**
([#85](https://github.com/adamkjonsson/python-zipline/issues/85)). The handle's
descriptor already fixes the layer, and `output_layer` was silently ignored
there, so a caller who asked for a sessionization stage with a handle got
whatever layer the handle had declared. It is now a `ValueError` before the
output is opened, as `params_digest` has been since `0.5.1`. Its default is
`None` rather than `DECODED`, so that an explicit `DECODED` is refused too;
a stage that declares its own decoder still defaults to the decoded layer. A
call that passed both now raises: pass the layer to `add_decoder()`, or give
`decode_stage` a decoder name.

**A pure ACK draws no edge, so a flow with stored pure ACKs merges and
verifies** ([zipline#160](https://github.com/adamkjonsson/zipline/issues/160)).
Through `0.22` every record had an end, its offset plus its length, so a
zero-length pure ACK ended at its offset. An ack reaching that offset drew an
edge from the pure ACK backwards to the data it answered, and the ordinary
handshake, data, pure-ACK trace made a cycle: `verify()` and the writer's
linearize guard refused a correct order, and `timeline()` stalled. `0.21` did
the same; nothing had met it because no producer stored pure ACKs. Since
`0.23` a record's end is one past the last sequence number it occupies:

- data: its offset plus its length, FIN or not;
- a zero-length SYN: its offset (the SYN occupies `isn`, one below);
- a zero-length FIN: its offset plus one;
- a pure ACK or bare RST: **none**, so no edge runs from it. Its own `ack`
  still orders it.

The merge also waits less: a peer record with no end now holds a record back
only for what could follow it, where it held back records whose ack it could
not answer. A merge that worked before gives the same order, except where it
stalled.

**`decode_stage` reads each input stream once to measure it, not twice**
([#91](https://github.com/adamkjonsson/python-zipline/issues/91)). The Session
End extents and the closing auto-fill measured every input stream separately;
they now share one measurement. The output is unchanged, byte for byte.

### Added

**`net:`, the fourth `content_type` scheme**
([zipline#153](https://github.com/adamkjonsson/zipline/issues/153),
[#156](https://github.com/adamkjonsson/zipline/issues/156)). A record labelled
`net:ip` or `net:ethernet` is exactly one IP packet or one Ethernet frame
(without the FCS), meaning what pcap's `LINKTYPE_RAW` and `LINKTYPE_ETHERNET`
mean. It is the label a packet-preserving stage gives its records, and the one
a generic consumer matches, whichever decoder wrote them; through `0.21` such
a stage could only use a decoder-private `dec:` token.

- `zpf.NET_IP` and `zpf.NET_ETHERNET` are the two labels, and
  `zpf.NET_LINK_TYPES` maps each token to its link type.
  `ContentType.is_net` and `ContentType.link_type` read a parsed label.
- `Record.content()` returns a `net:` payload as the label's meaning, so
  `strict=True` does not raise for it.
- `ContentRegistry.register_net(token, handler)` dispatches by token alone.
  Parsing a packet is beyond the format, so a handler's answer is the caller's
  claim, as for `mime:` and `dec:`.
- A `net:` token outside the vocabulary is opaque to a reader and refused to a
  writer, the way an illegal `prim:` token is. Nothing checks a `net:`
  payload against its label: the format parses no packets.

**`sessionize_stage`, a sessionization stage that fans out**
([#76](https://github.com/adamkjonsson/python-zipline/issues/76)). The hop
from a stream of packets to the conversations inside them, such as
`packets.zpf → inner.zpf` in the specification's decrypted-tunnel example.
`decode_stage` mirrors its input one to one under the same ids, and could not
express it; the only route was the primitive writer API, with coverage,
extents and timestamps done by hand.

- `stage.streams()` yields the input streams, and `stage.session(key,
  proto=…)` creates or returns the output session for a flow, with fresh ids.
  Its `participant(endpoint, isn=…)` is cached per endpoint, and its
  `record(participant, payload, cites=…, hints=…)` writes a transport record
  citing the input.
- Coverage is kept per input stream across every session, and auto-filled on
  close. Each session's Session End declares the whole extent of every input
  stream it cited. That extent is measured **once per input stream**, however
  many sessions cite it, and auto-fill shares the measurement
  ([#91](https://github.com/adamkjonsson/python-zipline/issues/91), contributed
  in [#92](https://github.com/adamkjonsson/python-zipline/pull/92)). The
  dev builds measured it once per session, a full pass over the input each
  time, which on a 200-flow fan-out was ten times the stage's other cost.
- Since `0.22` a record carrying `seq_start` may cite several input packets,
  or cross a break the input declares
  ([zipline#152](https://github.com/adamkjonsson/zipline/issues/152)). A
  record without `seq_start` may cite one range only, which is stricter than
  the format and said so in the docs.
- `InputStream` is the input side both stages share; `DecodeStream` extends
  it with its output pairing.
- `ts_first`, the first packet's arrival where `ts` is the last, is derived
  with `ts` from what a record cites, and written only for a record drawn from
  several input packets
  ([#86](https://github.com/adamkjonsson/python-zipline/issues/86)). It can
  also be passed explicitly, to `FlowSession.record` and now to
  `DecodeStage.record`, which never derives it.
  `StreamView.ts_first_for` is the stream-level mirror of `ts_for`.
- A flow key outlives its session
  ([#88](https://github.com/adamkjonsson/python-zipline/issues/88)): once a
  key's session has ended, `stage.session(key, …)` opens a new one on the same
  key, as the specification's `capture-gap` rule requires and an idle timeout
  or a reused port needs. `sessions()` lists every session in creation order,
  and an explicit `session_id` that contradicts the key's open session raises.

**`face` and `creator` on `decode_stage` and `sessionize_stage`**
([#89](https://github.com/adamkjonsson/python-zipline/issues/89)), passed to
`create()`, so a stage can write the JSON-Lines face and name its tool in the
File Header. The defaults write what the stages wrote before.

Rebuilt through it, `tunnel/inner.zpf` matches the vendored file block for
block (Source and Decoder ids aside, which are the writer's to number).

### Fixed

**A transport-layer `decode_stage` over a unit sequence wrote nothing**
([#84](https://github.com/adamkjonsson/python-zipline/issues/84)). Its
participants inherited the input's `adjacency = units`, which a writer MUST NOT
set on a transport participant, so the writer refused the stage's first
record. A unit sequence is what a tunnel decryptor writes, so this was the
sessionization path's main input. A transport-layer stage now declares
`contiguous`, and refuses an explicit `adjacency` other than that before the
output is opened.

**`check_splice` no longer reports a crossing `0.22` permits**
([#90](https://github.com/adamkjonsson/python-zipline/issues/90)). A
transport-layer record carrying `seq_start` MAY cite input packets either side
of a declared Discontinuity
([zipline#152](https://github.com/adamkjonsson/zipline/issues/152)): its
sequence numbers carry the break. `check_splice` reported every such record,
so a file `sessionize_stage` wrote, and the format allows, read as a
violation. A transport record without `seq_start`, and every decoded record,
still owe the duty and are still checked.

**`create()` refuses a stream of the wrong kind for its face**, before anything
is written. The binary face on a text stream, or the JSON-Lines face on a bytes
stream, used to fail on the first block with Python's own `TypeError`, far from
the call that chose the face. It is now a `ZpfError` naming the mismatch, in
the reader's words for the same case. `0.5.x` had it too; `face` on
`decode_stage` and `sessionize_stage` (#89) made it reachable there as well.
Found by python-zipline-wire.

## [0.5.2] - 2026-10-13

Implements spec **v0.21** (`SPEC_VERSION == (0, 21)`), unchanged from `0.5.1`.
A patch release: one additive keyword and one fix, no change to the bytes of
any file this library already wrote.

### Added

**`DecodeStage.undecoded(reason_class=…)`**
([#79](https://github.com/adamkjonsson/python-zipline/issues/79)). A decode
stage can now name a region with a reason of its own — `"decrypt-failed"`,
say, as the tunnel vector does — rather than only the canonical four.
`Writer.undecoded` has taken `reason_class` all along, and the specification
requires it with any reason outside the canonical vocabulary, but the stage
wrapper did not pass it on, so the writer's checker refused every specific
reason a stage wrote. Going around the stage to `stage.writer.undecoded` was
no answer: it skips the stage's coverage accounting, so auto-fill would name
the same bytes a second time. Validation stays with the writer, which owns
the canonical table and refuses a class that disagrees with a canonical
reason.

Prompted by zipline-kober's `fails_as`, which lets a spec name a failed
decryption.

### Fixed

**A refused `record()` or `undecoded()` no longer leaves a coverage gap.**
Both methods counted their ranges as covered *before* the writer saw the
block. A caller that caught the writer's refusal and carried on — the natural
way to retry with a `reason_class` — left those bytes counted but never
written, so auto-fill skipped them and the stage closed cleanly on a file
that failed `check_coverage`. The stage now counts a range only once its
block is written.

## [0.5.1] - 2026-09-27

Implements spec **v0.21** (`SPEC_VERSION == (0, 21)`), unchanged from `0.5.0`.
A patch release: one additive keyword, no behaviour change to any existing
call, and no change to the bytes of any file this library already wrote.

### Added

**`decode_stage(params_digest=…)`**
([#77](https://github.com/adamkjonsson/python-zipline/issues/77)). A decode
stage built through the orchestrator can now state the configuration its
decoder ran with — the reproducibility contract the Decoder Descriptor
carries. `FileWriter.add_decoder` has taken `params_digest` all along, but
`decode_stage` declares the Decoder itself and gave a caller no way to pass
one: a `DecoderHandle` is accepted as `decoder=`, and cannot be built
beforehand, because the writer it would come from is created inside the call.
So the documented entry point could not say what produced its output, and the
only way round it was to declare a second Decoder through `stage.writer` and
pass it on every record and region, leaving an unused descriptor in the file.

The keyword applies to a decoder the stage declares — a name or a
`(name, version)` pair — and is **refused with `ValueError`** alongside a
`DecoderHandle`, whose descriptor is already written. That is deliberately
stricter than `output_layer`, which is documented as *ignored* in the same
position: a digest names the run, so dropping it silently would lose exactly
the fact it was passed to record, while a layer that is already fixed on the
descriptor loses nothing. The refusal happens before the output file is
opened, so a rejected call leaves nothing behind.

Prompted by zipline-kober `0.5.0`, whose byte transforms (decompression, and
decryption with a caller's cipher) take document parameters such as a key: the
same spec with a different key decodes differently, and the digest is where
the specification puts that.

## [0.5.0] - 2026-09-18

Implements spec **v0.21** (`SPEC_VERSION == (0, 21)`), up from `0.20`. Files
written by `0.4.0` are refused at the version gate, and files written by this
release are unreadable by `0.4.0` — the `0.x` rule, as before.

`0.21` is a design release upstream, and unlike `0.20` it changes what a
reader computes: one rule the offset space was missing, and the first block
body change since `0.15`. No byte of any existing file changes and no extent
under 2 GiB moves — but this library read two of the new vectors wrong,
dropped a byte on re-encode of two more, and stayed silent on two negative
ones, so two rules are implemented in this port. Recorded in
`plans/SPEC-0.21-MIGRATION-PLAN.md`.

### Changed

**Offsets unwrap along stored order, so a transport stream can be placed past
2 GiB** ([zipline#146](https://github.com/adamkjonsson/zipline/issues/146)).
Through `0.20` every record was measured against the origin under serial
arithmetic, which read every record more than 2³¹ bytes into a stream as
*below the origin* — `stream-past-2gib` measured 1073741832 against a declared
3221225480. The rule now: a record's offset is its predecessor's plus the
**signed serial delta** of their `seq_start`s, the first record's is its delta
from the origin (`isn + 1`, or the first captured byte), and an **unplaceable
record anchors nothing** — the next record measures from the last placeable
one. `zpf.serial_delta` is the new primitive in `zpf.order`; `record_ranges`,
`stream_extent`, `StreamView.chunks()` and `units()` all walk through one
placer, so they cannot drift; and the checker's unplaceable note measures from
the same anchor, naming the origin or the predecessor, whichever the record
fell below. This closes the loop `0.4.0`'s #70 entry left open — "until the
format moves" — and inverts the two tests that pinned the old reading: four
records 1 GiB apart on an `isn`-less stream now place at 0, 1, 2 and 3 GiB
with nothing reported. Nothing under 2 GiB reads differently: the walk yields
`(seq_start − origin) mod 2³²` there.

**A Participant Descriptor carries `adjacency`, a body field**
([zipline#80](https://github.com/adamkjonsson/zipline/issues/80),
[zipline#106](https://github.com/adamkjonsson/zipline/issues/106)). The
reserved `u16` after `participant_id` is now `adjacency: u8` + `_reserved:
u8`. `zpf.Adjacency` has two values: `CONTIGUOUS` (`0`) — what every file ever
written holds there and meant, stored neighbours join unless a Discontinuity
says otherwise — and `UNITS` (`1`), declaring the participant a **unit
sequence**: its offset space is still the stored-order concatenation, every
record addressable and citable, but no two adjacent records may be assumed to
join. The wholesale form of a Discontinuity at every seam, for a stage that
reorders and for a decoder whose units decompose one another. It is the third
**load-bearing** enum: an unrecognized value is kept as a raw `int`, projects
as its number, and is isolated by the checker — never guessed, and never
defaulted to `contiguous`. `Participant.adjacency` defaults to `CONTIGUOUS`,
so a block built without naming it encodes the same twelve body bytes it
always did. **Every participant JSONL line now carries `"adjacency"`**, right
after `pid`, as the specification's own lines do; a line without it is
rejected, as a decoder line without `output_layer` is.

Before this port the parser read the byte into `_reserved` and the encoder
wrote `0`, so a re-encode — and any transform built on this library — turned
a unit sequence into a stream that splices, the exact failure the field was
put in the body to prevent. The harness was blind to it: the re-encode and
JSONL → binary tests compare dataclasses, and a dataclass with no field for the
byte compares equal on both sides. There is now a field, and a test that
`UNITS` survives `dataclasses.replace(...).to_bytes()` at byte 10.

**The producer face is a keyword.** `SessionWriter.participant(adjacency=)`;
`FileWriter.derive_from(adjacency=)` and `decode_stage(adjacency=)` override
the whole output. Every re-declaration carries the input's *effective*
adjacency forward: `UNITS` only where the input really was a unit sequence at
the decoded layer, because on a transport-layer participant the field says
nothing and a reader ignores it — copying the byte verbatim would turn an
inert value into a claim about the output. `merge_files`, transport-only,
always writes `contiguous`. `rewrite_decoded` keeps the per-seam form and
takes no override; a pass-through of `unit-sequence-reversed` through it
stays a unit sequence. `Seam`'s docstring points at the wholesale form.

**The consumer face is `StreamView.is_unit_sequence` and `units()`.** The
property is layer-aware when the view came from a reader (`False` for a
transport participant whatever its byte says) and falls back to the hint test
when the view was built by hand. `units()` reports a unit sequence the way it
reports everything about joins: a `Break` before every record after the
first, with the new `Break.declared` attribute set `False`, so a consumer that
flushes on every break honours a unit sequence without a second branch.
**This goes one step beyond the standard**, which forbids the splice and does
not say how a reader surfaces the prohibition; the flag is what tells a
synthetic break from a producer's, and the docstrings say so. `Break.declared`
defaults `True`, so existing constructions are unchanged.

**The conformance checker learns two conditions.** An `adjacency` this
version does not define isolates the Participant block — decidable there,
unlike `output_layer`, and its records then name an undeclared participant,
which is the "discard the Participant together with everything referencing
it" the specification describes. `units` on a participant whose records
resolve to the transport layer is **advisory**, in the shape of a
transport-layer label: reported once per participant at the first record that
settles the layer, ignored, and the file accepted. The checked writer treats
an advisory finding as a refusal, so a writer that sets `units` on a transport
participant is refused at that record — the writer's MUST NOT, with no extra
code. The seam predicate's first clause reads "decoded layer, not `units`": a
unit sequence asserts no join, so there is nothing for a missing block to
contradict, and the block stays permitted there.

**`capture-gap` is a Session End reason**
([zipline#147](https://github.com/adamkjonsson/zipline/issues/147)) — the
word for the one hole the walk cannot place, 2³¹ bytes or more between two
consecutive records: the producer ends the session there and opens another on
the same key with no `isn`. Open vocabulary, so nothing in the reader changes;
the word is in `SessionEnd.reason`'s docstring beside `capture-end`, with the
distinction (the capture stopped, or it resumed and the stream could not).

**The conformance suite grows by seven.** The vectors are re-vendored at
`v0.21` — 62 vectors, 70 files — and every case is in `KNOWN_PASSING`. Unlike
`0.20`, each new name was earned by a phase: `stream-past-2gib` and
`stream-wraps-seq` by the walk, `unit-sequence-reversed`,
`unit-sequence-nested` and `session-split-capture-gap` by the field's
projection, `isolate-unknown-adjacency` and `advisory-transport-adjacency` by
the checker. `DEFECTIVE` stays empty: the projection sweep found nothing, and
the new key was checked byte-against-`.jsonl` on all 61 Participant blocks.
Declared extents: 44 streams across 37 vectors, all asserted.

### Decided

**Package D is declined upstream, and nothing here is removed**
([zipline#125](https://github.com/adamkjonsson/zipline/issues/125)). The
`0.19` and `0.20` plans anticipated a release deleting `input_extents`,
`reason_class`, the `dropped` MUST and the seam predicate; `0.21` reverses
that decision, this library's having implemented every piece of it among the
reasons. `check_extents`, `check_coverage`, the `CoverageLedger` and the
predicate's two arms all stay. The `u64 offset` Record option for the
unmeasurable hole is deferred to a `1.x` minor, being safe to add later, so
`capture-gap` is the whole of the answer for now.

## [0.4.0] - 2026-09-17

Implements spec **v0.20** (`SPEC_VERSION == (0, 20)`), up from `0.19`. Files
written by `0.3.0` are refused at the version gate, and files written by this
release are unreadable by `0.3.0` — the `0.x` rule, as before.

`0.20` is a repair-and-vector release upstream: no option or block was added,
no body layout moved, and **no rule this library did not already keep was
introduced**. Two of its four headline items were filed from this repository.
The port is therefore the version stamp, the re-vendored vectors, and the
harness catching up to what the manifest can now say — recorded in
`plans/SPEC-0.20-MIGRATION-PLAN.md`.

### Changed

**`RecordFlags.RETRANSMIT` names the sender's act, not the reassembler's**
([zipline#142](https://github.com/adamkjonsson/zipline/issues/142)). A
duplicated capture — every packet seen twice through a mirror port or a veth
pair — is not a retransmission and does not set the flag; what the
reassembler discarded, retransmitted or duplicated, is an Undecoded block
against the capture, as it always was. Nothing in this library sets the flag,
so the change is the enum's docstring and one paragraph in *Concepts*; a
producer writing records through `zpf` reads the meaning from there.

**The conformance suite is full.** The vectors are re-vendored at `v0.20` —
55 vectors, 63 files — and every case is in `KNOWN_PASSING`, the first port at
which the ratchet was full on arrival. `DEFECTIVE` is empty: `0.20` fixed the
three vectors whose bytes disagreed with their own projection
([zipline#141](https://github.com/adamkjonsson/zipline/issues/141), found here
at the `0.19` re-vendor), and made upstream's `build.py` compare a vector's two
faces at registration so the class cannot recur. The new `merge/` fixture and
its negative twin `isolate-merge-unmarked-hole` pin the obligation
`merge_files` already met — a pass-through that cites a transport stream marks
its input's holes — and `check_extents` finds the twin's unmarked hole from the
output alone, as it did before the fixture existed.

**Declared extents are asserted for every accept vector.** `0.20` answered
[zipline#140](https://github.com/adamkjonsson/zipline/issues/140) with an
`extents` key on every single-file accept entry, stating per participant
stream the extent a reader must compute. The harness reads it, so the
hand-transcribed table it replaced is gone: 37 streams across 31 vectors are
now asserted against `zpf.stream_extent`, where two were.

**Python 3.10 is now supported** — `requires-python` is `>=3.10`, down from
`>=3.11` ([#67](https://github.com/adamkjonsson/python-zipline/issues/67)).
Nothing structural pinned the floor: the library used `typing.Self` at runtime
in one module, `datetime.UTC` in a few places, and `tomllib` in one test. Those
are now `TYPE_CHECKING`-only, `timezone.utc`, and a `tomli` fallback for the
test on 3.10 (a dev-only dependency; the package still has zero runtime
dependencies). CI runs the suite on 3.10 as well.

### Fixed

**A record below the origin of an `isn`-less stream is now reported**
([#70](https://github.com/adamkjonsson/python-zipline/issues/70)). The format
fixes the origin at the first captured byte when there is no `isn` and measures
everything from it, which `record_ranges()` always did — but the conformance
checker read "no `isn`" as "no floor" and returned early, so such a record was
zeroed by one path and noted by neither, and `reader.unplaceable` stayed empty
on a file that had lost bytes. The checker now applies the same floor from the
same origin, and the note names which one (`isn + 1` or `the first captured
byte`). The only stream that reaches this without an `isn` is one that has
carried more than 2 GiB — the ordering rule keeps neighbours within 2³¹, so the
only way below the origin is around it. That ceiling is the format's
([zipline#146](https://github.com/adamkjonsson/zipline/issues/146)), still open;
until it moves, the reader's two answers agree about it.

## [0.3.0] - 2026-09-06

Implements spec **v0.19** (`SPEC_VERSION == (0, 19)`), up from `0.16`. Files
written by `0.2.0` are refused at the version gate, and files written by this
release are unreadable by `0.2.0` — the `0.x` rule, working rather than
regressing.

### Changed

**Both `record()` signatures are grouped by the format's own lines**
([#59](https://github.com/adamkjonsson/python-zipline/issues/59)).
`SessionWriter.record()` went from twelve flat keywords to nine, and not by
inventing bags: the two new bundles are the transport and decoded layers, which
the format already separates and states rules across. The policy for where a
*future* option lands is written down in `docs/dev/option-exposure.md`.

| Was | Now |
| --- | --- |
| `record(…, seq_start=1001, ack=5001)` | `record(…, hints=zpf.Hints(seq_start=1001, ack=5001))` |
| `record(…, decoder=http)` | `record(…, decoded=zpf.Decoded(decoder=http))` |
| `record(…, content_type="dec:request")` | `record(…, decoded=zpf.Decoded(content_type="dec:request"))` |
| `record(…, spans=(span,))` | `record(…, decoded=zpf.Decoded(spans=(span,)))` |
| `DecodeStage.record(…, spans=(span,))` | `DecodeStage.record(…, cites=(span,))` |
| `DecodeStage.record(…, ts=segment.ts)` | omit `ts`; it is derived from `cites` |
| `create(…, single_clock=True)` | *removed* — the flag left the format in `0.19` |
| `begin_session(…, sequenced_basis="clock")` | *removed* — likewise |
| `participant(…, origin=…)` | *removed*; a pass-through writes an identity span per record |

`zpf.Hints` moved from `zpf.decode` to `zpf.writer`, both writers now using it;
it is re-exported as `zpf.Hints` either way. `DecodeStage.record()` keeps
`decoder=` and `content_type=` flat, its whole surface being the decoded layer
already, and `cites=` absorbs the old `spans=`: one parameter that takes offset
pairs and ready spans in any mix.

### Removed

- Options `origin` (`0x0064`), `sequenced_basis` (`0x0053`) and the File Header
  `flags` field (`0x0014`, whose only bit was `SINGLE_CLOCK`), which `0.19`
  removed from the format. `zpf.Origin`, `zpf.FileFlags` and
  `zpf.SEQUENCED_BASES` are gone with them. A file carrying any of those ids
  still round-trips it as a `RawOption`, which is the escape contract.
- `FileHeader.flags` and `FileHeader.single_clock`; `Session.sequenced_basis`.

### Added

- **`role` (`0x0092`) on Record**, the option `0.17` added for
  [#58](https://github.com/adamkjonsson/python-zipline/issues/58): what a
  decoded record **is**, in a vocabulary its decoder documents, independent of
  the `content_type` that says what kind it is. A decoder emitting one record
  per protocol field can now carry `prim:u32` *and* `"checksum"`; before, it
  had to choose between the type and the name. Available as
  `zpf.Decoded(role=…)` on `SessionWriter.record()` and as a flat `role=` on
  `DecodeStage.record()`, and carried forward by `rewrite_decoded()`.

  **`comment` goes back to being a note.** Using it to name a record was a
  stopgap — it is free text by definition, so a consumer parsing it depends on
  something the format says means nothing. `role` is opaque to the format too,
  but its scope is *declared*, which is what separates a name from a note.
- **`zpf.Decoded`**: a record's decoded-layer statement — its decoder, its
  `content_type`, its `role`, and the input ranges its bytes correspond to.
- **Per-unit timestamps** ([#62](https://github.com/adamkjonsson/python-zipline/issues/62)).
  `Segment.contributors`, `Segment.ts_for()`, `Segment.ts_first_for()`,
  `StreamView.contributions()` and `StreamView.ts_for()` make the specification's
  timestamp rule reachable: a decoded record carries the completion time of the
  last input record **in its span set**, per unit rather than per reassembled
  run. `DecodeStage.record()` derives it from `cites` when `ts` is omitted, so
  the normative answer is now the default one.
- **`FileReader.unplaceable`**: records the offset space could not place,
  reported in a list of their own. Since `0.19` such a record breaks no rule
  while a reader still SHOULD report it, so the file is reported clean and the
  record is reported anyway — which one list could not do.
- `zpf.Contribution`, the input record behind part of a `Segment`.
- `resolve_spans(…, hops=…)`: follow the provenance chain further than the
  record's own spans, or (`hops=None`) to the capture.

### Fixed

- **A record below the stream origin no longer wraps to 2³² − 1**
  ([#63](https://github.com/adamkjonsson/python-zipline/issues/63)). "Where does
  this record sit" was written three times and two of the three trusted the
  modular subtraction, so one below-origin record took the stream's extent with
  it. There is one definition now, and `record_ranges`, `chunks` and `units` all
  call it. `SessionWriter.record()` also refuses to *write* one — for a
  handshake record that is the format's MUST, and for a payload-carrying record
  it is **stricter than the format**, which the error says.
- `rewrite_decoded()` marks what it removes `dropped` rather than `skipped`,
  which since `0.17` is what makes a filter's seam duty checkable.
- `merge_files()` writes an identity span per record, marks its inputs' holes
  `gap`, and declares `input_extents` — a pass-through cites its input since
  `0.19`, and is answerable for its coverage.

## [0.2.0] - 2026-08-15

Implements spec **v0.16** (`SPEC_VERSION == (0, 16)`).

A break in both directions. Files written by 0.1.0 are stamped for spec 0.9
and are refused at the version gate, and files written by 0.2.0 are
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

[Unreleased]: https://github.com/adamkjonsson/python-zipline/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/adamkjonsson/python-zipline/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/adamkjonsson/python-zipline/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/adamkjonsson/python-zipline/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/adamkjonsson/python-zipline/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/adamkjonsson/python-zipline/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/adamkjonsson/python-zipline/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/adamkjonsson/python-zipline/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/adamkjonsson/python-zipline/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/adamkjonsson/python-zipline/releases/tag/v0.1.0

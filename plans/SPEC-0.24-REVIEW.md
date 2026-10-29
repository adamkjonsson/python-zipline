# Zipline Payload Format 0.24 — review

Review of the `0.24` release candidate on the
[`plan_0_24`](https://github.com/adamkjonsson/zipline/tree/plan_0_24) branch at
`66d2ec3`: the specification, the rationale companion, the CHANGELOG, the
release plan, and the 76 vectors. Written from `python-zipline`, a complete
implementation of `0.23` (all 68 of the `0.23` vectors pass). Line numbers refer
to `docs/zipline-payload-format.md` at `66d2ec3`.

This is the python-zipline read of the branch that the plan's Phase 7 asks for
before the PR is opened.

---

## Verdict

**Ready, after one fix to the text.** The release does what it says: each of
the six gaps has a rule or a vector, and the mechanical claims all check out
(see [Verified sound](#verified-sound)). The `0.23` vectors pass unchanged
against this implementation once its version is bumped, so nothing in `0.24`
moves under an existing reader.

**Six findings. One should be fixed before the tag, two are corrections to the
release's own record, and three are editorial or can wait.**

- **Finding 1** is the one that matters. The #170 paragraph says an incomplete
  reassembly leaves a trace because the coverage guarantee owes its fragments an
  Undecoded block. That is true for a stage reading a `.zpf`. It is not true for
  a reassembler reading a capture, which is the common producer, because a block
  against a `capture` source creates no obligation (line 1964). In that case a
  lost datagram can vanish without a trace, which is the outcome the withholding
  rule exists to prevent.
- **Findings 2 and 3** are about the release's description of python-zipline
  and about one definition that needs a contrast drawn.

---

## Findings

### 1. The trace for an incomplete reassembly is owed only to `.zpf`-sourced stages

**Where.** §Discontinuity, *An incomplete reassembly is not content of the
stream* (lines 2155–2172). The same claim appears in the companion's
Discontinuity section and in the vectors README's *Added in `0.24`*.

**What it says.** The fragments are not content of the stream, so leaving them
out withholds nothing. They "are still content of the stage's **input**, so the
coverage guarantee owes them an Undecoded block, and that block is the trace".
The paragraph ends: "A reassembler reading a capture rather than a `.zpf` names
the `capture` source, as it does for anything it discards". The companion
argues the same way. The withholding rule's premise (a file that withheld is
byte-identical to one that did not) does not hold here, because the block is
there.

**Why that does not hold for a capture.** The withholding rule binds "a stage
emitting a transport layer" (line 2149), which includes the head-of-pipeline
reassembler reading a pcap. For that stage, line 1964 says a block against a
`capture` source "discharges no coverage obligation, nor creates one … it records
what a stage discarded, and no rule consumes it". So nothing requires the block.
The sentence naming the `capture` source has no keyword, and the new `SHOULD`
pins only the block's `reason`, not whether the block exists.

**Consequence.** zpfwire over a pcap, the producer that raised #166 and #170,
can lose a fragment, write no record and no block, and conform. That file is
byte-identical to one that silently withheld a whole datagram. The scope
argument is sound for `.zpf`-sourced stages and does not reach this one.
`sessionized-fragment-lost` exercises only the `.zpf` path, so the suite does
not show the gap.

**Suggested fix.** One of these:

- **(a)** A reassembler that reads a capture **SHOULD** write an Undecoded
  block against the `capture` source for the fragments of a message it could not
  complete. That costs one `SHOULD` and keeps the paragraph's argument true for
  both producers. This is our preference.
- **(b)** Limit the claim to `.zpf`-sourced stages, and say plainly that a
  capture-sourced reassembler's file carries no guaranteed trace of an
  incomplete reassembly. This is honest, but it concedes the property for the
  most common producer.

Whichever is chosen, the companion paragraph and the README sentence ("Its
input range is still owed an Undecoded block") follow it.

### 2. The changelog and plan misdescribe python-zipline's #168 changes

**Where.** CHANGELOG `[0.24]`, the #168 entry's *Who changes*; plan D1,
*What this decides for python-zipline*.

- The changelog names python-zipline's `_extent_and_gaps` as the function that
  changes. That function was removed in python-zipline on 2026-10-23 (`1a8d293`,
  the 0.7.0 streaming work). The measurement is now
  `StreamView.extent_and_gaps`. It does still need the change: on
  `closed-final-ack` it measures the server's stream as **4**, where `0.24`
  says **5**.
- Plan D1 says "`check_coverage` is right". It is right about the extent (it
  measures 5), but it has no FIN exemption. Against `decoded-past-fin` it
  reports:

  ```
  input stream (source 1, session 7, pid 1): declared extent 5, but spans and
  Undecoded blocks account for only [0, 4) — the tail is silently dropped
  ```

  So both of python-zipline's computations change, in opposite directions. The
  changelog's generic sentence "A coverage check exempts the FIN's position"
  already covers this. Only the named function and the plan's "is right" need
  correcting.

**Suggested fix.** In the changelog, name `StreamView.extent_and_gaps`, or name
"python-zipline's decode stage" without a function. In the plan, say that
`check_coverage` measures the extent correctly and needs the exemption.

### 3. "Reach" and "end" differ for a zero-length FIN, and the text does not say so

**Where.** The new extent paragraph (line 850) and *Fields used*' end table
(lines 393–404).

`0.23` defined a record's **end** for the merge: a zero-length FIN ends at its
offset + 1. `0.24` defines the extent by how far a record **reaches**: a
zero-length record reaches its own offset. Both are right, and they differ on
exactly one kind of record. A reader that already has `_sequence_end`-style
code is likely to compute the extent as the furthest end and get it wrong by
one.

The suite already pins the difference. `merge-pure-ack-and-fin` and
`sequenced-pure-ack-and-fin` both end the client's stream with a zero-length FIN
at offset 8, and declare extent **8**. The furthest end is 9.

There is a second, smaller ambiguity: "a hole before the furthest record counts,
and so does the sequence number a FIN occupies" reads as unconditional until
the paragraph's last sentence qualifies it.

**Suggested fix.** One sentence after the definition, for example: *"This is
not a record's [end](#fields-used): a zero-length FIN ends one past its offset
but reaches only its offset, so a stream whose last record is a FIN ends where
the FIN sits."* Optionally, attach "where something is stored past it" to the
FIN clause.

### 4. Edges of the `copy` constraints

**Where.** Enums, *`copy` names the capture point's act* (lines 2617–2637).

- **A stage that keeps a copy but drops what it repeats.** A filter or time
  window that drops the original and keeps the flagged copy leaves a record
  whose flag either breaks the first `MUST NOT` or, if the new predecessor
  happens to have the same bytes, points at a different transmission. The
  `MUST NOT` binds the writer, so a careful stage clears the flag, but nothing
  says so. One clause would settle it: *"a stage that does not carry a copy's
  predecessor forward clears `copy`."*
- **The `seq_start` ban is per record, not per stream.** A record without
  `seq_start` in a sequence-anchored stream (the `unplaceable-no-seq-start`
  shape) may carry `copy` as written. If the intent is "never on a
  sequence-anchored stream", say *"on a record of a stream whose records carry
  `seq_start`"*.
- **The decoded layer.** What a decode stage does with a copy is #171 and is
  rightly out of scope. Until #171 is settled, it would help to say that the
  flag's meaning is defined at the transport layer only. Otherwise a pass-through
  of a decoded layer has to guess whether "its participant's preceding record"
  means a decoded record.

None of these blocks the release.

### 5. Session End's `extent` links to a section, not to the definition

Line 1752 says the extent is defined "as
[Layers](#layers-transport-and-decoded-live-in-separate-streams) defines it". Plan
Phase 2 checked that this "still lands on the definition", and it does, but on the
parent section about 160 lines above it. The new coverage paragraph (line 1142)
links straight to `#referencing-the-source-by-stream-offset`. Using that anchor in
both places would put a reader on the definition.

### 6. Vectors README: one unwrapped line

In *Multi-file fixtures*, the sentence listing the pair checks ("…
`decoded-past-fin/`, the two fragment pairs and the two across-break pairs on
top: …") runs as one long line, unlike the rest of the file.

---

## Verified sound

Checked mechanically against `66d2ec3`:

- **`check.py` is green.** It reports 76 vectors (67 single-file, 9 fixtures),
  35 options, 12 blocks and 48 rules, all exercised. The enumerations check (4
  sets, 12 sites) passes with the `copy` token.
- **The vectors are reproducible.** `build.py`, run in a clean copy, changes no
  byte.
- **`ruff check` and `ruff format --check` are clean.**
- **The keyword counts match a recount of both tags:** `MAY` 55 → 56, `MUST`
  124 → 126, `MUST NOT` 44 → 46, `SHOULD` 28 → 29. These are the changelog's
  numbers.
- **No stale version stamps.** No `0.23` remains in a format string, the File
  Header row, the worked example or the status note. The worked example's
  `version_minor` bytes are `18 00`.
- **The reserved mask is right.** `0xFE20` is `0xFF20` without `0x0100`, and
  `escape-reserved-flag-bit` keeps its meaning.
- **`isolate-copy-differs` is tiered consistently.** The `copy` paragraph's
  "MAY treat … as a semantic violation" restates the general licence of
  *Semantic violations — the reader MAY isolate* (line 3039), as line 1773 does.
- **The fixture count is right.** The README's "Nine directories are fixtures"
  matches the manifest.
- **The `message` permission (#166) is decidable.** It is keyed on something
  in the file, and `isolate-transport-splice` is a real negative for both kinds
  of evidence.

### Against python-zipline

With `SPEC_VERSION` bumped to `(0, 24)` and the vectors re-vendored in a
throwaway worktree, **all 68 `0.23` vectors still pass**. Of the new ones,
`closed-final-ack`, `decoded-past-fin/capture`, both `sessionized-*` pairs,
`sessionized-across-break` and `isolate-transport-splice` already pass. What
fails is expected port work (next section). None of it points at a defect in a
vector.

---

## Implementation impact (python-zipline)

What `0.24` will take here, for the record:

- **The `copy` flag**, `0x0100`, with its JSONL token, the two `MUST NOT`s on
  the write side, and an optional reader check. This is what fails the JSONL
  round-trip on `udp-capture-copy` and `isolate-copy-differs` today.
- **The FIN position in the extent.** `StreamView.extent_and_gaps` reaches a
  zero-length record's offset (4 → 5 on `closed-final-ack`), and
  `check_coverage` exempts the FIN's position (Finding 2).
- **The test harness.** Multi-file `merged` items carry a `file` key, so the
  harness fails on `chain` and `decoded-past-fin`. The exact case count goes from
  76 to 89 files.

---

## How this was reviewed

Read the CHANGELOG `[0.24]` entry, the release plan in full, and the diff of the
specification and companion against `v0.23`. Ran `check.py`, `build.py` (in a
copy), `ruff`, and a keyword recount of both tags. Read the manifest entries and
README rows of the new vectors, and the `.jsonl` of `closed-final-ack`,
`merge-pure-ack-and-fin` and `udp-capture-copy`. Then ran python-zipline's
vector suite against the `0.24` vectors in a throwaway worktree with only the
version bumped. Finding 2 and the reach/end evidence in Finding 3 come from that
run. Finding 1 comes from asking which producer the #170 argument depends on,
then reading the `capture`-source rules it relies on.

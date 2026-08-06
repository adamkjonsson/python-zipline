# Conformance vectors

Small `.zpf` files, each with its expected JSON-Lines projection or its expected
failure, for testing an implementation of the
[Zipline Payload Format](../docs/zipline-payload-format.md) `0.14`.

Run `python3 check.py` to verify the tree is self-consistent.
Run `python3 build.py` to regenerate it.

Both use **only the standard library**, deliberately: regenerating or verifying
the vectors must not depend on anything being installed. The linter is the one
development dependency — `pip install -r ../requirements.txt`, then
`ruff check .` from the repository root.

## Ground rules

These four decide whether the vectors are worth anything, so they come first.

**1. They are hand-built from the specification, never dumped from an
implementation.** [`build.py`](build.py) spells out every block field by field
from the normative text. It is not a reader or a writer for the format. The
point of a vector is to catch an implementation diverging from the specification
— and a vector generated *by* that implementation cannot, because it encodes
whatever the implementation already gets wrong. The `time_units` defect that
prompted this work would have been captured in a vector and blessed.

*Independent evidence that the builder is right:* `raw-minimal` is byte-for-byte
identical to the 196-byte worked example in the specification, which was written
by hand years apart from this code. That exercises the frame, TLV framing,
padding, and every body layout it touches.

**2. They are subordinate to the specification, not a second source of truth.**
If a vector and the normative text disagree, **the vector is wrong** and gets
fixed. Anything else recreates in a new form the exact failure these exist to
prevent — an implementer copying an artifact instead of reading the text.

That said, a disagreement is *investigated* before it is dismissed. A vector that
contradicts the text usually means the text is ambiguous, which is the second
thing vectors are for. One such ambiguity was found while writing these: the rule
said a repeatable option renders as a JSON array, while every example rendered a
single `endpoint` as a scalar string. That is now settled — always an array.

**3. The JSONL side is compared semantically, not byte-for-byte.** The projection
is *semantically* lossless, so a converter is free to choose key order within a
line, and number-versus-decimal-string encodings for 64-bit fields. But **line
order is significant** — declare-on-first-use and stored record order both carry
meaning.

So the comparison is: same number of lines, in order; each line parsed and
compared as an object; the documented encoding equivalences normalised first. A
harness that diffs the raw text will report false failures on its first run.

**4. Negative vectors matter more than positive ones.** Valid-file/expected-
projection pairs are the easy half. The format has a two-tier error model, and
the tiers are where implementations differ:

| Tier | Meaning |
|------|---------|
| `accept` | A conformant file. The `.jsonl` is the expected projection. |
| `reject` | Structurally corrupt. A reader MUST reject the whole file. |
| `isolate` | Well-framed but semantically invalid. A reader MAY reject the file *or* discard the smallest unit it can soundly isolate — and MUST NOT silently repair, guess, or drop without a diagnostic. |

Rejecting an `isolate` vector, with a diagnostic, **is conformant** — the tier
table above and *Conformance* both permit it. The failures to guard against are
different: a reader that accepts a semantic violation **silently**, and one that
treats an `accept` vector as corrupt. A reader that rejects an unknown block type
fails the extension contract outright — see the `escape-*` vectors, which are all
`accept`.

**A negative vector carries exactly one violation.** With two, it silently tests
whichever the reader detects first, and passes implementations that never
exercised the rule it was built for. `isolate-coverage-gap` carried two through
`0.12` — the coverage gap it exists to test, and a missing `produced_by` — so a
reader could pass it without implementing coverage checking at all. Fixed in
`0.13`.

Since `0.13` this is **enforced, not just stated**. Every vector declares a
`violations` count in [`manifest.json`](manifest.json), and `check.py` requires it
to agree with the tier — 0 for `accept`, 1 for `reject`, 1 for `isolate`. The
count is declared in [`build.py`](build.py), never computed by inspecting the
file: a checker that ruled on semantics would become a second normative
authority, which ground rule 2 forbids. Declaring it is mandatory, so a new
vector cannot be written without confronting the number, and adding a second
defect to an existing one fails the build rather than quietly weakening it.

**Every capability the format defines is exercised by some vector**, and since
`0.14` that is enforced too. `check.py` parses the option-id registry and the
block-type table out of the specification and requires each entry to appear in
some vector, so **new syntax cannot ship uncovered**. Rules — permissions with no
id to derive, like session fan-out — are declared in `check.py`'s `RULES` beside
the vector exercising each.

It hard-fails rather than warning. Session fan-out shipped in `0.13` as a
*Clarified* item with nothing exercising it, and the gap survived a whole release
until an implementation reviewed it; an advisory line is exactly what gets
scrolled past. What a vector exercises is recorded by [`build.py`](build.py),
which built the bytes, so the record cannot drift from them — and `check.py` still
parses no block body, which would make it the conformant reader ground rule 2
forbids.

## Layout

Each vector is a directory:

```
<name>/<name>.zpf     the file under test
<name>/<name>.hex     annotated hex dump, generated from the same description
<name>/<name>.jsonl   expected projection (accept vectors only)
```

The `.hex` dump exists so a change to a vector is reviewable in a diff — a raw
`.zpf` is an opaque blob. It is generated, never edited, so it cannot drift from
the bytes. [`manifest.json`](manifest.json) is the machine-readable index: name,
tier, size, the specification section each comes from, and what a reader must do.

## The vectors

### Baseline

| Vector | What it exercises |
|--------|-------------------|
| `raw-minimal` | The whole container in 196 bytes. Identical to the specification's worked example. |
| `file-clock-metadata` | The File Header's clock options: `time_epoch` moves the origin, so wall time is `(time_epoch + timestamp) / tick_hz`; **SINGLE_CLOCK** asserts one trustworthy clock across the file — a clock assertion, *not* an ordering one. |
| `descriptive-metadata` | Four optional pass-through options, one per block that defines one: `link_type`, `flow_key`, `identity`, `ts_first`. None changes how anything else is read; the point is that a converter carries them rather than dropping them. |
| `custom-block` | A Custom (`0xFF`) vendor block. **Recognised, not unknown** — a reader skips it by length, but a converter projects `pen`, `subtype` and a base64 `payload` rather than routing it through the unknown-block escape. |
| `decoded-basic` | A decode stage: `spans` provenance, `content_type`, an Undecoded tail, an End block. Its Session End declares `input_extents`, so the coverage guarantee is checkable from this file alone — and it is the suite's only **Session End** block. |
| `passthrough-transport` | A pass-through preserving a transport layer: `origin`, byte-run records, `SEQUENCED`. |
| `broken-chain` | The provenance walk that **fails**: a `zpf-input` Source naming a `missing.zpf` that is deliberately not in the tree, with a bytes-exist Undecoded block pointing into it. `accept` tier — the file breaks no rule; what is absent is a sibling. See [the provenance chain](#the-provenance-chain). |

### The four escapes

All `accept`. These are the forward-compatibility contract, and the place a
naive implementation most often fails by treating extension as corruption.

| Vector | What it exercises |
|--------|-------------------|
| `escape-unknown-block` | An undefined block type — skipped by `length`, projected as a hex type plus base64 `content`. |
| `escape-unknown-option` | An unregistered option id — skipped by `len`, retained in the generic `options` array. |
| `escape-unknown-enum` | An undefined `tcp_role`. Advisory, so it renders as the raw number. |
| `escape-reserved-flag-bit` | A set reserved bit in a record's `flags` — ignored semantically, preserved as a hex token. |

### Ordering and sequencing

| Vector | What it exercises |
|--------|-------------------|
| `annotator-decoded` | A pass-through preserving a **decoded** layer — the construct `0.9` could not express. Records keep `decoder_id` and carry no `spans`; the inherited Undecoded block forces the grandparent Source to be declared. |
| `passthrough-discontinuity` | The **two re-emission rules side by side**: an inherited Undecoded block copied *verbatim* (its statement is about a file further up the chain) next to a Discontinuity *renumbered* to this file's ids (its statement is about the stream carrying it). The input's `(7, 0)` becomes `(42, 1)`, so a verbatim copy is visibly wrong rather than accidentally right. |
| `session-fan-out` | **One input stream demultiplexed into two output sessions** — the capability `0.13` clarified and nothing exercised. Its `[0,80)` is spanned by *both* sessions, since one ciphertext record's framing fed an inner unit in each, so the spans **overlap** — legal since `0.14`, where coverage became *at least once*. Neither session covers the extent 200 it declares; only the union across both does. **A checker that accumulates coverage per output session fails here and passes every other vector in the suite.** |
| `undecoded-skipped` | `reason = skipped` for a deliberately-declined region (a BOM). |
| `undecoded-reason-class` | A non-canonical `reason` carrying the required `reason_class`. |
| `sequenced-basis` | A hint-less `SEQUENCED` session with its mandatory `sequenced_basis`. |
| `hintless-merge-backwards-ts` | A hint-less session whose timestamps run backwards *across* participants. Every record is concurrent, so the whole order is the merge's tie-break — but each participant's own records keep stored order. A reader that rejects, or re-sorts within a participant, fails. |
| `reordered-decoded` | A stage that reorders decoded records without decoding them — a decode stage, since stored order defines the offsets. **Its `spans` run downward against stored order**, which a reader assuming they ascend will fail. |
| `merge-timestamp-tie` | Two concurrent records from different participants with **identical timestamps**, stored in the *opposite* order to the one the merge must produce. Before `0.12` this tie was unresolved and two conformant readers could disagree; the tie-break is now ascending `participant_id`. |
| `partially-hinted-sequenced` | A `SEQUENCED` session where **one** record carries `seq_start` and the rest carry nothing. A single hint anywhere means the session is not hint-less, so no `sequenced_basis` is required — even though most of the order rests on timestamps. Pins the answer to the question that took longest to settle. |

### Added in `0.13`

| Vector | What it exercises |
|--------|-------------------|
| `external-session-id` | A session carrying an identity assigned outside the format — a 16-byte binary UUID. The value is **opaque bytes, not a string**: it projects as base64 and must not be spelled out, or one id acquires two spellings. |
| `discontinuity-unknown-width` | A `tls-records` stage that lost a TCP segment. The plaintext length is unknowable, so the Discontinuity carries **no `width`** and contributes 0 to the positional arithmetic — output offsets stay `[0,50)` and `[50,80)`. The block's job is to say those two records **do not join**. |
| `discontinuity-known-width` | A QUIC stream decoder, where the gap **can** be counted. `width = 25` is a term in the arithmetic, so the second record occupies `[75,105)`, not `[50,80)`. **A reader that skips the block, or reads it but ignores `width`, computes a different range for every later record** — the failure the block exists to prevent. |

### Reject tier

| Vector | Defect |
|--------|--------|
| `reject-bad-magic` | Byte-swapped magic. |
| `reject-unknown-major` | `version_major` this reader does not implement. |
| `reject-unknown-minor` | **The minor after this one** while major is 0 — `build.py` derives it from the version the tree stamps, so it always names a version no reader implements. **The vector that distinguishes a `0.x`-aware reader from one assuming minors are always skippable.** |
| `reject-length-misaligned` | A block `length` that is not a multiple of 4. |
| `reject-payload-len-overrun` | `payload_len` running past its own block. |

### Isolate tier

| Vector | Violation |
|--------|-----------|
| `isolate-undeclared-session` | A record naming a session that was never declared. |
| `isolate-duplicate-id` | A `source_id` declared twice. |
| `isolate-coverage-gap` | A decode stage leaving an input range neither covered by `spans` nor marked Undecoded. |
| `isolate-sequenced-no-basis` | A hint-less `SEQUENCED` session with no `sequenced_basis`. Recording is unconditional — the trivially-sound cases write `trivial` rather than omitting it. **A reader can only raise this at Session End**, since hint-lessness is a property of the records. |
| `isolate-unknown-source-kind` | An undefined Source `kind` — load-bearing, unlike `tcp_role`, because it decides how span offsets are read. |
| `isolate-extent-exceeds-coverage` | A Session End declaring an input stream 40 bytes long while `spans` plus Undecoded blocks account for only `[0,20)`. A **trailing** gap — invisible without `input_extents`, which is what distinguishes it from `isolate-coverage-gap`'s interior one. |
| `isolate-extents-disagree` | Two output sessions drawing on one input stream, declaring **different** extents for it — 200 and 160. An input stream has one length, and under fan-out every consuming session declares that whole length, so the two Session Ends contradict each other. Only reachable once fan-out is legal. |
| `isolate-discontinuity-in-raw` | A Discontinuity block in a **raw** file. A transport offset space is already hole-inclusive, so the sequence numbers and a declared `width` are two accounts of the same missing bytes, with no rule for which to believe. |

## Multi-file fixtures

Two directories are **fixtures** rather than vectors: several files that only mean
anything together, because what they test is a relationship *between* files.
`check.py` walks each member exactly as it walks a single-file vector — framing,
projection, the lot — and adds arithmetic specific to `chain/` on top.

### The splice fixture

`splice/` is the negative one. It exists because Finding 3 of the `0.13` review
cannot be expressed in one file:
the break lives in stage 1's *output*, so a reader handed only stage 2 has
nothing to detect the violation from.

```
tls-records.zpf   stage 1, conformant     http.zpf   stage 2, the violation
  record   [0,50)                           record spans [0,80) of stage 1's
  BREAK at 50, no width                     output -- straight across the
  record   [50,80)                          break -- declaring nothing
```

Tier `isolate`, and the violation belongs to **the pair**: `tls-records.zpf`
breaks no rule, and `http.zpf` is well-framed with complete coverage and nothing
wrong on its face. That is the point — it is only judgeable with its input in
hand, and a harness that tests files individually will pass it.

It is the conformance test for the MUST NOT added in `0.14`: a decode stage
reading an input that carries a Discontinuity must not emit a unit whose `spans`
cross it without declaring one of its own.

### The provenance chain

`chain/` is the positive one: three files whose digests and offsets genuinely
agree, so the things a single file cannot exercise become testable.

```
cap.pcap ──[sessionize]──▶ raw.zpf ──[http decode]──▶ decoded.zpf
                                                           │
                                                   [annotate]──▶ annotated.zpf
```

The byte budget is fixed and small enough to check by hand: session 7 carries
`pid 0` with a 9-byte request, and `pid 1` with a 16-byte response head plus a
4-byte tail the decoder cannot parse. So `decoded.zpf` covers `[0,9)` of pid 0
and `[0,16)` of pid 1 with records, and `[16,20)` with an Undecoded block —
exactly the 20 bytes `raw.zpf` holds.

What only this fixture can test:

- **The recovery walk.** Following `decoded.zpf`'s Undecoded block back to real
  bytes in `raw.zpf`, rather than to a `uri` that resolves to nothing.
- **Two-hop resolution.** `annotated.zpf`'s records carry no `spans`, so
  answering "which raw bytes is this?" means going through `decoded.zpf` —
  while its inherited Undecoded block reaches `raw.zpf` in one hop. The
  asymmetry is the point.
- **Digest verification.** Each declared digest is the real SHA-256 of the
  sibling file it names, so a reader can verify the chain instead of assuming it.
- **The coverage guarantee, actually checked.** Coverage is stated against the
  *input's* streams, and nothing in a decoded file records how long those were —
  so a lone decoded file cannot be verified. Here the parent is present, so
  `check.py` reconstructs `raw.zpf`'s extents from `seq_start - (isn + 1)` and
  confirms `decoded.zpf` accounts for every byte. That workaround is what
  `input_extents` (`0x00C1`, added in `0.13`) removes the need for: a file now
  declares its inputs' lengths itself, so the same arithmetic works on a lone
  decoded file. The fixture keeps the cross-check honest.

**Its counterpart is [`broken-chain/`](broken-chain).** This fixture is the walk
that *succeeds*; that vector is the walk that fails, citing a `missing.zpf` that
is deliberately not in the tree. Together they cover the distinction `0.10` made
normative — *no bytes exist* versus *bytes unavailable* — which a consumer MUST
NOT report identically. Note it is an `accept` vector: the file breaks no rule,
and what is absent is a sibling, so the requirement it tests is about a
consumer's recovery walk rather than about the file.

## Coverage this does not have

Stated so nobody mistakes the suite for complete:

- No causal-merge vectors: no skewed two-file capture, no tie-break case.
- No truncation vectors, which need a file that ends mid-block.
- No tunnelled `endpoint` list, no `spans` chunked across several occurrences,
  no `Custom` block, no Session End.
- Payloads are short and ASCII; nothing exercises the 64-bit fields near 2⁵³
  where the decimal-string encoding becomes mandatory.

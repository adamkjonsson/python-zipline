# Conformance vectors

Small `.zpf` files, each with its expected JSON-Lines projection or its expected
failure, for testing an implementation of the
[Zipline Payload Format](../docs/zipline-payload-format.md) `0.16`.

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

**One violation does not isolate.** Since `0.16` an `accept` entry may also carry
`"advisory": true`, and then declares **1** violation rather than 0. It is the
shape of an advisory MUST NOT — the file breaks a rule, the reader reports it and
carries on, and nothing is discarded. That is neither `accept` (which means the
file is clean) nor `isolate` (which means a reader may drop something), and it is
a key rather than a fourth tier because the tier names what a *reader does*, and a
reader accepts these files completely. `advisory` on any other tier fails the
build, since where a reader may discard something the word says nothing.

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
| `raw-minimal` | The whole container in 196 bytes. Identical to the specification's worked example. A capture-sourced transport stream — the name predates `0.15` retiring "raw" as a normative term, and is kept because it is an identifier. |
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
| `undecoded-skipped` | `reason = skipped` for a deliberately-declined region (a BOM). Also **the case that must carry no Discontinuity**: a discarded BOM withholds nothing, so the text either side joins. A duty keyed on unspanned input bytes would demand a block here, and it would be a lie. |
| `undecoded-reason-class` | A non-canonical `reason` carrying the required `reason_class`. |
| `sequenced-basis` | A hint-less `SEQUENCED` session with its mandatory `sequenced_basis`. |
| `hintless-merge-backwards-ts` | A hint-less session whose timestamps run backwards *across* participants. Every record is concurrent, so the whole order is the merge's tie-break — but each participant's own records keep stored order. A reader that rejects, or re-sorts within a participant, fails. |
| `reordered-decoded` | A stage that reorders decoded records without decoding them — a decode stage, since stored order defines the offsets. **Its `spans` run downward against stored order**, which a reader assuming they ascend will fail. Since `0.15` its one seam carries a Discontinuity (`reason = reordered`, no `width`): the stage withholds nothing, but stored neighbours assert that they join and these two never did. |
| `merge-timestamp-tie` | Two concurrent records from different participants with **identical timestamps**, stored in the *opposite* order to the one the merge must produce. Before `0.12` this tie was unresolved and two conformant readers could disagree; the tie-break is now ascending `participant_id`. |
| `partially-hinted-sequenced` | A `SEQUENCED` session where **one** record carries `seq_start` and the rest carry nothing. A single hint anywhere means the session is not hint-less, so no `sequenced_basis` is required — even though most of the order rests on timestamps. Pins the answer to the question that took longest to settle. |

### Added in `0.13`

| Vector | What it exercises |
|--------|-------------------|
| `external-session-id` | A session carrying an identity assigned outside the format — a 16-byte binary UUID. The value is **opaque bytes, not a string**: it projects as base64 and must not be spelled out, or one id acquires two spellings. |
| `discontinuity-unknown-width` | A `tls-records` stage that lost a TCP segment. The plaintext length is unknowable, so the Discontinuity carries **no `width`** and contributes 0 to the positional arithmetic — output offsets stay `[0,50)` and `[50,80)`. The block's job is to say those two records **do not join**. Paired since `0.15` with `isolate-unmarked-break`, which is this file with the block deleted. |
| `discontinuity-known-width` | A QUIC stream decoder, where the gap **can** be counted. `width = 25` is a term in the arithmetic, so the second record occupies `[75,105)`, not `[50,80)`. **A reader that skips the block, or reads it but ignores `width`, computes a different range for every later record** — the failure the block exists to prevent. |

### Added in `0.15`

| Vector | What it exercises |
|--------|-------------------|
| `sessionization-stage` | **The cell F0 left empty: a `zpf`-sourced *transport* stream.** A reassembler run over a `.zpf` input, its Decoder declaring `output_layer = transport` in its **body**. Records carry `spans` **and** `decoder_id` like any decode stage's, but everything else is a transport stream: `isn`, `seq_start`, hole-inclusive offsets, no `content_type`. The 25-byte hole is expressed by the sequence numbers, **not** by a Discontinuity, which a transport-layer stream still may not carry whatever produced it. |
| `reassembler-declared` | The head-of-pipeline reassembler taking up the **SHOULD**: capture-sourced, with a Decoder declaring `transport`. The same logical layer `raw-minimal` holds unlabelled — both conformant, which is the deliberate asymmetry `0.15` records rather than leaves to be rediscovered. Records carry no `spans`: a capture is not a `.zpf`. |
| `isolate-unknown-output-layer` | *(isolate)* An `output_layer` value this version does not define. The load-bearing twin of `isolate-unknown-source-kind` — the value decides whether offsets are hole-inclusive positions or a payload concatenation, so a reader cannot compute a single record's range and MUST NOT guess. Being a body field there is no absent case to confuse it with. |
| `proxy-decoded` | **Case G — a decoded stream with no predecessor file.** A TLS-terminating proxy: records carry `decoder_id` and reference a **`capture`** Source, with no `spans` and no `origin`, because the bytes their units were computed from were never written to a `.zpf`. The cell the two axes were conflated to forbid. The coverage guarantee does not apply — it is scoped per *input* participant stream and there is none — and the Decoder is a claim of **identity, not a recipe**: nothing can regenerate this output. |
| `undecoded-in-capture` | An Undecoded block in a **capture-sourced** file, its offsets byte offsets into the capture. The stage is the *reassembler*, declaring an overlapping retransmit it discarded. Barred before `0.15` on the assumption that capture-sourced meant no transform had run — and reassembly is a transform. The stream stays at the transport layer, so its gap is expressed by sequence numbers and it still may not carry a Discontinuity. |
| `mixed-derivation` | One derived file with a **decode-stage stream beside a pass-through stream**: session 10 carries `spans`, session 11's participant carries `origin`. Before `0.15` a derived file was exactly one of the two, which left a tool with a decoder for one protocol and not the other two dishonest options — pass everything through, or mark the second stream entirely Undecoded, **dropping those bytes from the output**. The replacement rule binds per participant, not per file. |
| `filtered-decoded` | A **filter**: it keeps two decoded records and drops the one between them. The dropped region is Undecoded `skipped` — the same value a discarded BOM carries, which is the ambiguity [#78](https://github.com/adamkjonsson/zipline/issues/78) raised. What tells the two apart is not the reason but whether the survivors still **join**, and here they do not, so the seam carries a Discontinuity. Its `width` is **declared** as 40: a filter knows the length of what it dropped, and an absent width would claim otherwise. Declaring it keeps the output offset space aligned with the input's. |

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
| `isolate-discontinuity-in-raw` | A Discontinuity block on a **transport-layer** stream. That offset space is already hole-inclusive, so the sequence numbers and a declared `width` are two accounts of the same missing bytes, with no rule for which to believe. Since `0.15` the bar is the layer rather than the file kind; the vector's name keeps the retired word because harnesses reference it. |
| `isolate-self-derived` | **Intra-file derivation**: `spans` naming a `zpf-input` Source whose `uri` is this very file, with the stream they claim to come from sitting beside them. Only reachable once mixed-state files are legal, which is what makes it worth pinning. A stage reads its input and then writes its output, so a file cannot be among its own inputs. **Detection is partial by design** — the only signal is the `uri`, so a reader handed a *path* may compare and isolate, while one handed a file object cannot and is not obliged to. |
| `isolate-unmarked-break` | **Finding 3 as one file**, and the vector this suite most needed: a decode stage whose own output breaks, saying nothing. It is `discontinuity-unknown-width` with the Discontinuity deleted and nothing else changed. Every other rule is satisfied — coverage is *complete*, because the guarantee is about the input and has no opinion on the output — so a checker that only accumulates ranges passes it. **Under `0.14` this file was conformant.** Unlike `splice/` it needs no second file: a `hole`-class Undecoded region between the input regions of two adjacent output units is the one shape of the duty decidable from a single file. |

### Added in `0.16`

Three isolate vectors for MUSTs the syntax already let you break, and the suite's
first **advisory** vector.

| Vector | Violation |
|--------|-----------|
| `isolate-mixed-layer-participant` | *(isolate)* **One participant, two layers**: a record whose decoder declares `decoded` beside one whose decoder declares `transport`. Every other rule holds — both decoders declared, coverage complete — which is the point: under `0.15` this file broke nothing stated. The layer fixes the stream's **offset space**, and this stream has two incompatible answers for it. Mixing *decoders* per record stays legal; mixing the **layers they declare**, within one participant, does not. |
| `isolate-unbound-zpf-stream` | *(isolate)* A `zpf`-sourced participant that is **neither created nor preserved**: no `origin`, and its record carries no `spans`. Nothing says which stream inside the input its bytes came from. The two ways of producing a `zpf`-sourced stream are exhaustive and `0.15` never said so — the discriminator forbade being *both* and was silent on being *neither*, which is how `isolate-self-derived` shipped carrying this as a second, unintended violation. |
| `isolate-hole-against-capture` | *(isolate)* A **`hole`**-class Undecoded region against a `capture` Source. `undecoded-in-capture` is the conformant shape of the same block; this is the class it may not use, because the reassembled stream is a transport layer whose hole-inclusive offsets already carry the gap. Two accounts of the same missing bytes with no rule for which to believe — the contradiction that also bars a Discontinuity there. |
| `advisory-transport-content-type` | *(accept, **advisory**)* A transport-layer record carrying `content_type: prim:bytes`. A **MUST NOT**, and the only one whose violation is advisory: dropping the label loses nothing and the record stays readable, so there is no unit a reader could soundly discard. **Rejecting or isolating this file is not conformant.** A reader ignores the label, reports it, and MUST NOT conclude the stream is decoded — which would put every later offset in that participant in the wrong space. |

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

Read it beside `isolate-unmarked-break`, which is its other half. This fixture
tests **carrying** a break down a chain; that vector tests **originating** one.
`0.13` shipped only the first, so the head of every chain was unobliged and
Finding 3 stayed conformant until `0.15` — which is what
[#78](https://github.com/adamkjonsson/zipline/issues/78) reported.

### The provenance chain

`chain/` is the positive one: three files whose digests and offsets genuinely
agree, so the things a single file cannot exercise become testable.

```
cap.pcap ──[sessionize]──▶ raw.zpf ──[http decode]──▶ decoded.zpf
                                                           │
                                                   [annotate]──▶ annotated.zpf
```

`raw.zpf` is a **transport-layer** stream. Its filename predates `0.15` retiring
"raw" as a normative term, and is kept for the same reason `raw-minimal`'s is —
harnesses reference it. The specification's own worked example, which used to use
the same name, calls it `transport.zpf` since `0.16`
([#99](https://github.com/adamkjonsson/zipline/issues/99)); the two are separate
artifacts and nothing requires them to agree.

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

### The tunnel

`tunnel/` is the biggest, and the one that walks the whole of `0.15` at once —
#41's case D, a decrypted WireGuard tunnel in four files, one direction:

```
wg.pcap ──[capture]──▶ outer.zpf ──[wireguard-decrypt]──▶ packets.zpf
                       UDP datagrams                      inner IP packets
                       capture / transport                zpf / decoded

packets.zpf ──[tcp-reassembly]──▶ inner.zpf ──[http/1.1]──▶ http.zpf
                                  two TCP flows             messages
                                  zpf / TRANSPORT           zpf / decoded
```

Tier `accept`; all four are conformant. Four things it is built to show, each of
which was an argument somewhere in `0.13`–`0.15`:

- **Correspondence, not identity.** An inner packet spans the *whole* outer
  datagram — nonce and tag included, because those fed the computation — so tunnel
  coverage closes with **no `skipped` blocks at all**. The earlier estimate was one
  per packet for framing; that clarification is what made case D affordable.
- **Fan-out.** One input stream (`packets.zpf` session 5, pid 0) feeds **two**
  output sessions in `inner.zpf`. Neither covers `[0,150)` alone and both Session
  Ends declare the same extent 150 for the shared input — the rule
  `isolate-extents-disagree` polices, here in its legitimate form.
- **A `zpf`-sourced transport stream.** `inner.zpf`'s Decoder declares
  `output_layer = transport`, so a *decode stage's* output is `isn`-anchored and
  hole-inclusive. Its 40-byte hole shows up as `seq_start 1081` where 1041 would be
  contiguous — no Discontinuity, no `content_type`. **Nothing else in the suite
  shows the F1 cell against a real gap.**
- **Origination, and the crossing left undone.** `packets.zpf` withholds an inner
  packet and declares the break. `inner.zpf` reads that break and *cannot* express
  one, so it does the other legal thing — no record crosses packets offset 100 —
  and the loss survives as a TCP sequence gap instead. `http.zpf` then meets a
  `hole`-class region between two adjacent output units and originates its own
  block. Delete that one block and `http.zpf` **is** `isolate-unmarked-break`.

`check.py` walks it specifically, as it does `chain/`: every declared digest
against the real sibling, each hop's coverage against its predecessor — the middle
one accumulated across *both* output sessions — and `inner.zpf`'s flow-A extent
re-derived from `seq_start - (isn + 1)` and compared with what `http.zpf` declares.

## Coverage this does not have

Stated so nobody mistakes the suite for complete:

- No causal-merge vectors: no skewed two-file capture, no tie-break case.
- No truncation vectors, which need a file that ends mid-block.
- No tunnelled `endpoint` list, no `spans` chunked across several occurrences,
  no `Custom` block, no Session End.
- Payloads are short and ASCII; nothing exercises the 64-bit fields near 2⁵³
  where the decimal-string encoding becomes mandatory.

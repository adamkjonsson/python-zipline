# Zipline Payload Format 0.15 — review

Review of the [0.15 specification](https://github.com/adamkjonsson/zipline/blob/v0.15/docs/zipline-payload-format.md),
its [CHANGELOG](https://github.com/adamkjonsson/zipline/blob/v0.15/CHANGELOG.md)
and the 49 vectors at tag `v0.15`. Written from `python-zipline`, a complete
implementation of `0.14` (binary container, JSON-Lines projection, conformance
checker, causal merge, decode-stage / pass-through / filter transforms,
provenance walk, cross-file splice check; all 39 of the `0.14` vectors pass).

Fourth round. The `0.10`–`0.14` findings were all adopted, and this one is
written the same way: nothing here challenges the normative model, and every
suggested fix is local.

---

## Verdict

**Implementable, and worth implementing — but two findings block a from-the-text
implementation today.** `0.15` is the first release that had to change what
existing files *mean*, and the two-axis model it replaces the raw/derived
conflation with is right. The `proxy-decoded` cell — a decoded stream with no
predecessor `.zpf` — was genuinely unencodable under `0.14`, and the two
workarounds the manifest names (drop `decoder_id` and call TLS-proxy output byte
runs, or fabricate a predecessor) are both worse than the format admitting it had
the axes wrong. `output_layer` as a **body** field, numbered so that `decoded = 0`
lands on the old `_reserved` bytes, is the neatest thing in the release: the
Decoder Descriptor body stays four bytes, every `0.14` writer already wrote the
correct value, and the "not safe to skip" argument against making it an option is
the same argument the Discontinuity block earned in `0.13`. That checks out at the
byte level against `isolate-self-derived.hex` and the rest.

**Ten findings. Two block implementation, three are corrections, five are
editorial.**

The two blocking ones are the same shape as `0.14`'s dominant defect, which is
worth naming because `0.14` was the release written to stop it: **a rule restated
in several places, with only some copies updated.**

- The closing paragraph of §Undecoded still says a transport-layer stream carries
  no Undecoded blocks "because no decoder ran". Two of this release's own new
  accept-tier vectors — `undecoded-in-capture` and `sessionization-stage` — are
  transport-layer streams carrying Undecoded blocks. The paragraph was edited in
  this release for vocabulary and not for the rule stated 60 lines above it in the
  same section (**Finding 1**).
- The Undecoded block's field table, its namespace rule and its offset rule all
  still assume a `zpf-input` Source, and `undecoded-in-capture` cannot be parsed
  in a way that satisfies all three. Its `session_id` is *this file's*, its
  offsets are (per the manifest) capture-file byte offsets, and the block's own
  hex annotates that `session_id` as "in the input's namespace" when it is not
  (**Finding 2**).

The third correction is the one I would want settled before anyone writes a
checker: **nothing requires the records of one participant to resolve to the same
layer** (**Finding 3**). The layer fixes the stream's offset space, but is
computed per record through `decoder_id`, and mixing decoders per record is
explicitly legal. One participant can hold a `transport` record and a `decoded`
record and break no stated rule, at which point its offset space has two
incompatible answers.

And one observation about scope rather than correctness: **the duty this release
exists to add is not mechanically checkable in the case that motivated it.**
`filtered-decoded` and `undecoded-skipped` are byte-shaped identically — a
bytes-class `skipped` region between two adjacent output units — and one owes a
Discontinuity while the other does not. The specification says so plainly and is
right that only the producer knows. But the manifest's own line, "`skipped` does
two unrelated jobs, and only one of them is a break", describes a vocabulary
problem the release chose not to fix, and fixing it would make issue #78's own
title case checkable (**Finding 5**).

**Performance for readers: no measurable cost on the read path, a small and
bounded cost for validating readers.** Details in
[Performance for readers](#performance-for-readers).

---

## Findings

### 1. §Undecoded still says transport-layer streams carry no Undecoded blocks

**Severity: high — the text contradicts two of the release's own accept-tier
vectors.**

The second paragraph of §Undecoded is new in `0.15` and legalises the case:

> The input is usually a predecessor `.zpf`, and the block appears wherever a
> stage names one. That includes a **capture-sourced** stream, where the input is
> the capture itself and the stage is the reassembler.

The closing paragraph of the same section still says the opposite:

> A **transport-layer** stream, however it was produced, expresses its TCP gaps
> *implicitly*, as a discontinuity between consecutive records' sequence
> numbers — **neither carries Undecoded blocks, because no decoder ran** and the
> pass-through re-emits its input's records, gaps included.

Both clauses of that sentence are now false:

| Vector | Layer | Decoder? | Carries Undecoded? |
|---|---|---|---|
| `undecoded-in-capture` | transport, capture-sourced | none anywhere | **yes** |
| `sessionization-stage` | transport, `zpf`-sourced | `output_layer = transport` | **yes** (`gap`, `[50,75)`) |

The sentence *was* touched in this release — "A **raw** file, or a
**pass-through** preserving a transport layer" became "A **transport-layer**
stream, however it was produced" — so the copy was updated for the retirement of
"raw" and not for the rule that changed two paragraphs earlier. It reads as the
authoritative statement because it is the last word in the block's own section,
which is the first place an implementer looks.

**Fix.** Rewrite it to: a transport-layer stream expresses gaps in its offsets
rather than with a Discontinuity, *and* may carry Undecoded blocks naming regions
of an input it declined or discarded; the two mechanisms answer different
questions (the output's shape versus the input's accounting). Delete "because no
decoder ran".

### 2. `undecoded-in-capture` cannot be parsed from the text — three rules disagree

**Severity: high — blocking. I cannot write the parser branch without a ruling.**

The block that ships is:

```jsonl
{"type":"undecoded","source_id":1,"session_id":7,"pid":0,
 "off_start":4096,"off_end":4396,
 "reason":"overlap-discarded","reason_class":"bytes"}
```

`source_id 1` is a `capture` Source (`tap.pcap`). Three normative statements bear
on it and no reading satisfies all three:

| Statement | Where | What it requires here |
|---|---|---|
| "`source_id` — the input Source (**`kind = zpf-input`**) whose stream the offsets index" | Undecoded field table | this block is illegal |
| "`session_id`/`participant_id` are in the referenced *source's* id namespace … **never the current file's**" | §Undecoded | a capture has no id namespace; `7` **is** this file's session id |
| "for a `capture` source, they are **byte offsets into the capture file** and `session_id`/`pid` are **unused (write 0)**" | span-list rule, TLV registry | `session_id` must be `0`, not `7` |

Against that, §Undecoded also says "Offsets are logical 0-based stream offsets in
the `source_id` input, the same convention used by `spans`", while the manifest
says "The block's offsets are **byte offsets into the capture file**, which is
what a span into a capture Source has always meant." Those are different
statements, and the file's own `.hex` annotates offset `0x00E8` as
`source_id = 1  (in the input's namespace)` for ids that are demonstrably this
file's.

The reading also has to survive the `hole` class. A reassembler's honest
capture-side declarations are "overlap I discarded" (bytes exist in the pcap) and
"a segment I never saw" (no bytes anywhere — the whole point of the `hole` class).
A **capture-file byte offset cannot name a segment that was never captured**, so
the byte-offset reading makes half the new capability unreachable, which is
presumably why the vector's ids are this file's own.

**Fix — I'd take the stream-offset reading and make the text say it.** Namely:
against a `capture` Source, an Undecoded block's `session_id`/`participant_id`
name **this file's own** stream and the offsets are that stream's logical
offsets — because the region being declared is a region of *this* stream that the
reassembler could not fill, not a region of the pcap. Then: fix the field table's
`kind = zpf-input`, carve the exception out of the "never the current file's"
sentence, and add the corresponding carve-out beside the span-list rule so the
"one struct parses both" claim keeps a matching pair of readings. If instead the
byte-offset reading is intended, the vector must be corrected to write `0` for
`session_id`/`pid`, and §Undecoded must say the `hole` class is unavailable
against a capture Source.

Either way one artifact has to change. As it stands `undecoded-in-capture` is the
first vector since the `0.12` round I cannot implement from the specification.

### 3. Nothing requires one participant's records to agree on a layer

**Severity: high — silently wrong offsets, and no rule broken.**

The layer is a property of a *stream*: it decides whether the offset space is
hole-inclusive true positions or a payload concatenation. But `0.15` computes it
per **record**, through `decoder_id → output_layer`, and mixing decoders per
record stays explicitly legal ("One decode stage MAY also mix *decoders*
per-record"). Nothing then forbids:

- one participant holding a record whose decoder declares `transport` beside one
  whose decoder declares `decoded`;
- one participant holding a decoder-less record (transport, by the layer rule)
  beside a record carrying a `decoded` decoder.

Both files pass declare-before-use, coverage, and every other stated rule, and
their offset space has two incompatible definitions. A reader computing
`input_extents` or resolving a downstream `spans` entry against such a stream gets
a number, and the number is meaningless.

This is not hypothetical bookkeeping: the layer rule is now *the* thing a reader
must get right, and the release's own framing ("**the unit is the stream, not the
file**") is exactly the invariant the syntax does not enforce.

**Fix, minimal:** one sentence in §Conformance — *all records of one participant
MUST resolve to the same layer; a file whose participant mixes layers is a
semantic violation a reader MAY isolate.* One line in a checker, and it makes
"the stream's layer" well defined rather than merely intended.

**Fix, structural** — see [Better solutions](#better-solutions-considered): the
layer arguably belongs on the Participant Descriptor, not on the Decoder. I don't
recommend it for `0.15`, because the reason the Decoder placement was chosen —
`decoded = 0` lands on bytes every existing writer already wrote — does not
transfer, and the participant `_reserved` bytes cannot encode the distinction for
old files. But the consistency rule above is then mandatory, not optional.

### 4. The one mechanically decidable case is stated without its layer qualifier, and without a predicate

**Severity: high — a checker written from the paragraph rejects a shipped
accept-tier vector.**

§Discontinuity states the checkable core as:

> Where an Undecoded region of the **`hole`** class lies between the input regions
> of two adjacent output units, no other reading is available … A checker may
> raise that from the file alone.

`sessionization-stage` is exactly that shape and is correctly conformant:

```
record A  spans [0,50)      (input session 4, pid 0)
undecoded [50,75)  reason=gap   ← hole class, between the two
record B  spans [75,105)
```

It carries no Discontinuity, because it is a **transport** stream and forbidden
one. The exemption is stated — a paragraph earlier, keyed on "any stream whose
offsets are the concatenation of its own record payloads" — but not restated where
the checkable rule is given, and the checkable rule is the one an implementer
copies into code. `tunnel/inner.zpf` is the same trap in the flagship fixture.

The second half is the predicate itself. "Between the input regions of two
adjacent output units" is not decidable without saying:

- **which** input stream — units may span several `(source_id, session_id, pid)`
  triples, and fan-out means adjacent output units may cite different ones;
- **how** to reduce a span *set* to a region, given that `spans` may overlap
  (legal since `0.14`, and `session-fan-out` ships it) and may run **downward**
  against stored order (`reordered-decoded` ships that, and its manifest entry
  says so explicitly: "A reader that assumes spans ascend fails here").

**Fix.** State the rule as a predicate, and put the layer test first. What I will
implement, absent a ruling:

> For each output participant, for each adjacent pair of records `(r1, r2)` in
> stored order, and each input stream `S` cited by both: let `A = max(off_end)`
> over `r1`'s spans on `S` and `B = min(off_start)` over `r2`'s spans on `S`. If
> `A < B` and some `hole`-class Undecoded region on `S` intersects `[A, B)`, a
> Discontinuity between `r1` and `r2` is required. The check applies only to
> decoded-layer output streams, and `A ≥ B` (a reordering or overlapping stage)
> disables it for that pair.

That passes all 49 vectors as far as I can tell on paper. It is a guess at what
the sentence means, and two implementations will guess differently — which is the
one thing a conformance suite exists to prevent.

### 5. The duty's motivating case stays unverifiable, and the `skipped` ambiguity is left in the vocabulary

**Severity: medium — design, not defect. Worth reopening before `1.0`.**

Two shipped vectors are byte-shaped identically and disagree on whether a block
is owed:

| Vector | Undecoded between two adjacent units | Discontinuity |
|---|---|---|
| `undecoded-skipped` | `reason = skipped`, bytes-class (a discarded BOM) | **not owed** — the content joins |
| `filtered-decoded` | `reason = skipped`, bytes-class (a dropped record) | **required**, `width = 40` |
| `tunnel/packets.zpf` | `reason = decrypt-failed`, bytes-class | **required** |

The specification is right that the *reason word* must not decide it — the test is
whether content that belonged between the two units reached the output, and only
the producer knows. But that leaves issue #78's own title case, the filter, with
no checkable statement at all: a filter that drops a record and stays silent is
indistinguishable from a decoder that discarded a BOM, and `isolate-unmarked-break`
covers only the `hole` class.

The manifest names the problem itself — "`skipped` does two unrelated jobs, and
only one of them is a break" — and then keys the fix on the block rather than on
the vocabulary.

**Suggestion.** Split the job the way `reason_class` already splits recoverability:
give the bytes-exist class two canonical words — `skipped` (withheld; the
survivors join) and `dropped` (content removed; they do not) — or add a
`breaks: bool` option on Undecoded. Either makes the filter case decidable from
one file, turns `filtered-decoded` into a *positive* conformance test of the new
duty rather than an example of it, and costs one vocabulary entry. It does not
close the general case, and nothing can; it closes the case the release was
opened for.

### 6. `isolate-self-derived` is not detectable by a reader that does not know its own name

**Severity: medium — an isolate-tier vector with no stated detection procedure.**

The rule is right and the reasoning for it is sound. Two problems with what
shipped:

**The justification rests on an optional field.** "`spans` name a Source carrying
a `digest`, and no file can contain its own hash" — but `digest` is an *option*
("a `zpf-input` source carries `uri`/`digest`", no MUST), and the vector's own
digest is the placeholder `sha256:0000`. The impossibility argument is about a
field a writer may omit; the prohibition needs to stand on its own.

**The only usable signal is `uri`.** The vector's Source is
`uri = "isolate-self-derived.zpf"`, matching the filename. A reader handed a path
can compare after normalisation; a reader handed a file object — which
`zpf.open()` accepts, and which is how stdin, a socket and a tar member arrive —
cannot. There is no in-band self-identifier.

**The vector carries a second, independent violation.** Session 21's participant
is `zpf`-sourced (its record references `source_id 1`, a `zpf-input` Source),
carries **no `origin`**, and its record carries **no `spans`** — neither created
nor preserved, which §Conformance already forbids. The manifest records
`"violations": 1`. A reader can isolate this file for entirely the wrong reason
and appear to pass the vector, which is the failure mode a negative vector exists
to avoid.

**Fix.** State the detection procedure (compare a `zpf-input` `uri` against the
path the file was opened from, when one is known; otherwise the condition is
unverifiable and a reader is not obliged to detect it), and give session 21 an
`origin` so the file's only violation is the one it is named for.

### 7. The transport-layer exemption assumes the transport has sequence numbers

**Severity: medium — an unrepresentable case, newly reachable.**

The exemption is stated over all transport streams:

> a transport stream is exempt for the mirror-image reason, its hole-inclusive
> offsets having already expressed the break.

That holds where the offsets are anchored by sequence numbers. It does not hold
for a message-oriented or `N=1` transport stream — UDP, a chat feed — where a
reader computes offsets by accumulating payload lengths and there is no `isn` and
no `seq_start` to say a datagram is missing. `tunnel/outer.zpf` is precisely such
a stream: UDP, no `isn`, four `message` records.

Before `0.15` this was academic, because a transport stream was a capture's
reassembled output and nothing dropped from it. `0.15` makes it reachable: a
filter or a stage declaring `output_layer = transport` may now withhold content
from a UDP transport stream, and has **no conformant way to say so** — the offsets
cannot express it, and the Discontinuity is forbidden by layer.

**Fix.** Either scope the exemption to transports carrying sequence information
and permit a Discontinuity where they do not, or say that a stage emitting a
transport layer MUST NOT withhold content from a stream whose offsets are not
sequence-anchored. The first is more useful; the second is a smaller change.

### 8. "Every existing `.zpf` re-reads correctly unmodified" is not true of a conformant `0.15` reader

**Severity: low — editorial, but it reads as a compatibility promise.**

The `output_layer` entry says "**No existing file changes — not one byte**" and
"every existing `.zpf` re-reads correctly unmodified". The byte-level claim is
correct and I verified it: the Decoder body stays 4 bytes, `decoded = 0` lands on
bytes a conformant `0.14` writer MUST have written 0.

But a conformant `0.15` reader **MUST reject `version_minor = 14`** at the header
gate, so no existing file re-reads at all. The property is real and worth having —
a `0.14` Decoder body, re-stamped, is already correct — but stated as written it
contradicts the `0.x` reject rule the same document opens with.

The same qualifier applies to the release's headline, "the first that changes what
already-written files mean": inside `0.x` that has no operational content except
for files re-stamped to `0.15`, which is exactly what happened to
`reordered-decoded`. Worth one clause in the changelog so the two claims don't
have to be reconciled by the reader.

### 9. "A transport-layer record carries no `content_type`" is not written as a MUST

**Severity: low.**

The prose is emphatic and the argument (a reassembly record's boundaries are a
slice, not a unit) is correct. But it is stated descriptively, the option registry
still reads `content_type … Record (decoded)`, and §Conformance's isolate list
mentions "a block appears where its kind is forbidden" without naming this case.
A checker needs to know three things the text does not say: whether it is a MUST
NOT, whether a violation is isolating or advisory, and what a reader does with the
label if it appears.

**Suggestion.** MUST NOT, and **advisory** — dropping the label loses nothing and
the record remains fully readable, so the `tcp_role` treatment fits better than
the `origin`-in-a-capture-file treatment. `python-zipline` already separates those
two strengths (`AdvisoryError` vs `SemanticError`) and I will file it as advisory
unless told otherwise.

### 10. `transform_params_digest` is now justified by a reason `0.15` disproves elsewhere

**Severity: low — editorial.**

Two sentences ship in the same document:

- §Undecoded / changelog: "The old bar assumed capture-sourced meant no transform
  had run; **reassembly is a transform, and a destructive one**."
- §`transform_params_digest`: "**A capture-sourced stream is not the output of a
  transform**, so a file holding nothing else MUST NOT carry the option."

The outcome is right — a declared reassembler's configuration belongs on its
Decoder's `params_digest`, which `reassembler-declared` demonstrates — but the
stated reason is the one this release spent an entry refuting. Rephrase it as the
placement rule it actually is.

Two smaller things in the same area: "a file holding nothing else" is doing a lot
of work for three words and should say "a file all of whose streams are
capture-sourced"; and `proxy-decoded` sets `produced_by`/`produced_at` on a file
with no `zpf`-sourced stream, which the registry describes as belonging to derived
files. Legal, but currently unstated.

---

## Verified sound

Checked closely, no finding:

- **`output_layer` as a body field, and `decoded = 0`.** The reasoning is the
  strongest in the release, and the byte-compat claim holds: body size unchanged
  at 4 bytes, value 0 already written everywhere. The "not safe to skip" argument
  for a body field over an option is the same one the Discontinuity block earned,
  and is correct — a retained-but-ignored `output_layer` would read a transport
  stream's offsets as a payload concatenation, silently.
- **The two-axis model, and retiring "raw".** Both directions of the old
  implication really were false, and `proxy-decoded` was unencodable under `0.14`.
  The vocabulary change is more invasive than a rename (it lands in our public API
  — see below) and is still the right call.
- **Per-participant created-vs-preserved, replacing per-file purity.** The right
  unit, and `mixed-derivation` is the honest encoding of a tool that has a decoder
  for one protocol and not the other. The discriminator — a participant MUST NOT
  both carry `origin` and hold records carrying `spans` — is checkable in a single
  pass with state freed at Session End.
- **The origination duty keyed on "do these two join?"** rather than on unspanned
  input bytes or on `spans` adjacency. The decryptor case proves the alternatives
  wrong: nonce and tag are spanned but undecoded and the plaintext joins perfectly,
  so a coverage-keyed rule would demand a block asserting something false. The
  four-row table is the clearest normative statement in the document.
- **`tunnel/inner.zpf` declining the crossing.** This is the subtlest thing in the
  fixture and it is right. The reassembler cannot see the lost inner packet — the
  datagram carrying it failed to decrypt and is opaque — yet it produces a
  correctly-sized 40-byte hole, because the *next* inner packet's TCP sequence
  number (1081 where 1041 would be contiguous) reveals both the gap and its width.
  A stage that cannot express its input's break in its own output leaves the
  crossing undone; the information survives one layer down in a mechanism that
  predates the format. Worth reading before implementing anything else in `0.15`.
- **`sequenced_basis` not promoted to unconditional.** The undecidability argument
  for the split variant is correct — the Session Descriptor is written before its
  records, so a streaming producer cannot know whether the order followed from
  hints — and matches what we hit implementing the merge. Filing it as a
  *documented non-adoption* rather than silently dropping it is the right handling.
- **The `at least once` coverage guarantee under fan-out** survives the new mixed
  files unchanged, because it was already keyed on the input stream
  `(source_id, session_id, pid)` rather than on the output session. `0.14`'s fix
  paid off here.

One thing to watch rather than fix: because the coverage guarantee is scoped
"within each input participant stream", a decode stage that declares a `capture`
Source has no input streams and is therefore exempt — the `proxy-decoded` shape.
That exemption is correct for a real proxy and is also a self-declared opt-out of
coverage checking. No vector tests that a stage with a genuine `.zpf` predecessor
cannot take it, and none can, since the two are indistinguishable from inside one
file. Not a defect; worth knowing it is there.

---

## Performance for readers

**Short answer: no measurable cost on the read path; a small, bounded cost for
readers that validate.** This is analysis, not measurement — no `0.15` reader
exists to benchmark yet.

**Nothing on the byte-shovelling path changes.** No new block on the hot path, no
new option on the Record block, no change to record framing or to payload
handling. A reader that streams payloads out of a 10 GB `.zpf` does exactly the
work it did under `0.14`. `output_layer` costs one `u8` read per Decoder
Descriptor, of which a file typically has one or two.

**Layer resolution adds one lookup per record that carries a `decoder_id`.**
Under `0.14` the question "is this record decoded?" was answered from the record
alone; now it is `decoder_id → output_layer`. Precompute the set of transport
decoder ids once when the Decoder blocks are parsed and it collapses to a set
membership test — tens of nanoseconds in CPython, against roughly a microsecond
already spent parsing a record's options. Under 1% for realistic records, and it
disappears entirely if the layer is cached per participant, which the consistency
rule in Finding 3 would license.

**Single-pass streaming survives, which is the important part.** Decoders are
subject to declare-on-first-use, so the table is populated before any record
references it; no buffering, no second pass, no back-patching. This was not
guaranteed by the design — a layer field discovered *after* the records it governs
would have forced a validating reader to buffer a whole stream — and the release
gets it right by placing the field where declare-on-first-use already reaches.

**Validating readers pay in three small places:**

1. **File kind is no longer a single scalar.** Our checker locks one of
   raw/decode-stage/pass-through per file and short-circuits on it. `0.15`
   replaces that with per-participant classification: a dict entry per live
   participant, freed at Session End. Bounded, and the same shape as the
   per-session state we already carry.
2. **The fast path for capture-sourced files disappears.** Under `0.14`, "this
   file is raw" retired coverage bookkeeping for the rest of the file.
   `mixed-derivation` and `undecoded-in-capture` mean a validating reader must
   keep the coverage ledger alive until it knows no stream will need it. Cost is
   the ledger's interval lists per cited input stream — which decode-stage files
   already paid — extended to files that turn out to be mixed. Live memory stays
   O(cited input streams), freed at Session End.
3. **The unmarked-break check is genuinely cheap.** Under the predicate in
   Finding 4 it needs, per output participant, the previous record's per-input
   span extents plus the hole-class Undecoded regions seen since — O(1) state per
   live participant, O(spans) work per record. It does not need the whole file,
   which is a real design win: the checkable core of the release's flagship duty
   is streaming-friendly.

**Two things that would have hurt and don't.** The layer is resolvable before the
first record of a stream, so offsets never need recomputing. And the Discontinuity
still carries no offset field, so its position still comes from stored order —
meaning a reader that computes a stream's offset space already has the running
total the block needs, and cross-file splice checking (`zpf.check_splice`) needs
no new state.

**Non-readers pay more.** A *producer* of decoded output now has to answer "do
these two units join?" at every seam, which is knowledge it must carry through the
decode rather than a check it can run at the end. That is the point of the release
and it is the right place for the cost, but it is a real change in what a writer
API has to ask of its caller — see below.

---

## Better solutions considered

**The layer on the Participant Descriptor rather than the Decoder.** The layer is
definitionally a property of a stream; `0.15` attaches it to a per-record
reference and then has to state, in four places, that the unit is the stream.
Putting a `layer` field on the Participant body would make Finding 3 impossible to
express, remove the lookup entirely, and let a reader know a stream's offset space
from its Participant block without parsing Decoder Descriptors at all.

I do **not** recommend it, and the reason is the one the specification gives for
its own choice: `decoded = 0` on the old `_reserved` bytes makes every existing
Decoder Descriptor already correct. The Participant body's `_reserved` cannot do
that — old capture files would want `0 = transport` and old decoded files
`0 = decoded`, and one field cannot be both. The specification picked the
placement that preserves existing bytes over the placement that models the domain,
which is defensible; it just needs the consistency rule to close the gap the
choice opens.

**Making the filter case checkable via the reason vocabulary** — Finding 5. This
is the one I would actually change. The release correctly refuses to let the
reason word *decide* the duty, then leaves the vocabulary unable to *record* the
answer, and the result is that #78's title case ships with a normative MUST and no
vector that can test it.

**A back-reference on the Discontinuity block.** Considered and rejected: an
optional option naming the input region the break corresponds to would make the
correlation exact and remove the "between the input regions" guesswork of
Finding 4 entirely. But it duplicates what the neighbouring Undecoded block
already says, it is meaningless for the `reordered` case (there is no input region
— the units were simply never adjacent), and `0.13` already argued correctly that
the Discontinuity is a statement about the file's *own* output space and should
carry nothing read against the input. Stating the predicate is the cheaper fix.

**Requiring the head-of-pipeline reassembler to declare itself.** The release
makes it a SHOULD and records the resulting asymmetry rather than hiding it, which
is right. A MUST would make every existing capture-sourced file non-conformant for
saying nothing new — and inside `0.x`, where those files are rejected at the
version gate anyway, it would have bought a cleaner model at the cost of an
upgrade obligation for every producer. SHOULD plus a written-down asymmetry is the
better trade, and the note that "a consumer cannot conclude an undeclared
transport stream had no reassembler, only that none was named" is the sentence
that makes it safe.

---

## Implementation impact

Nothing here is hard. Sizing against our modules, assuming Findings 2 and 4 are
resolved first — those two are blocking, the rest can proceed in parallel.

**Binary and blocks** (`binary.py`, `blocks.py`): Decoder body `_reserved: u16` →
`output_layer: u8, _reserved: u8`; new `OutputLayer` enum with the load-bearing
treatment already used for `SourceKind` (unknown value isolates, MUST NOT guess,
raw number preserved through a round-trip). Small.

**JSONL** (`jsonl.py`): `decoder` lines always carry `"output_layer"`, both
directions. Every golden `.jsonl` regenerates. Mechanical.

**Reader** (`reader.py`, `reassembly.py`): two public API breaks, both real.
`FileReader.file_kind` returns `"raw" | "decode-stage" | "pass-through" | None`;
all three words are wrong under `0.15` and the question itself is now per-stream,
so it is removed rather than redefined, replaced by a per-participant accessor
giving `(provenance, layer)`. And `zpf.is_decoded_stream` — both the free function
and `SessionReader.is_decoded_stream` — answers from `decoder_id` alone, which
`0.15` makes half the question. The free function's signature is
`(records: Sequence[Record]) -> bool` and cannot be fixed in place: resolving the
layer needs the Decoder table, which a record sequence does not carry. It takes a
decoder-lookup argument or it goes. `record_ranges` and `stream_extent` sit
directly downstream of it and inherit the change.

**Conformance** (`conformance.py`): the largest change. `_lock_kind` /
`_require_derived` file-purity machinery is replaced by per-participant
classification (`_RAW`/`_DECODE`/`_PASS_THROUGH` and the `_orphan_participants`
deferral all go). New rules: Undecoded permitted on capture-sourced streams;
`decoder_id` permitted on capture-sourced records; Discontinuity keyed on layer
rather than on file kind (`isolate-discontinuity-in-raw` keeps its name and
changes its reason); unknown `output_layer` isolates; `content_type` on a
transport record (advisory, per Finding 9); the participant `origin`-vs-`spans`
discriminator; the unmarked-break predicate; the self-derivation check, guarded on
whether a path is known. Roughly a day, plus the layer-consistency rule if
Finding 3 is adopted.

**Transforms and reassembly** (`transform.py`, `decode.py`, `reassembly.py`): the
decode-stage helper needs a seam API — the producer must answer "do these join?"
rather than remember to emit a block, which is the only way an ergonomic writer
can enforce a duty that rests on producer knowledge. The filter helper gains a
declared `width`. The reassembly helper can now declare itself as a decoder with
`output_layer = transport` and emit Undecoded for discarded overlaps. New
capability: a sessionization stage over a `.zpf` input, which `0.14` could not
express at all.

**Docs** (`docs/`, `README.md`, `DECODER_API.md`, `CONTENT_TYPE_API.md`): "raw" is
retired throughout, including in prose that explains the model. Larger than a
`sed`, since several passages explain *why* raw and derived line up — which is the
thing that stopped being true.

**Vectors**: 39 → **49**, no removals. Ten new (`filtered-decoded`,
`isolate-self-derived`, `isolate-unknown-output-layer`, `isolate-unmarked-break`,
`mixed-derivation`, `proxy-decoded`, `reassembler-declared`,
`sessionization-stage`, `tunnel`, `undecoded-in-capture`) and one changed
(`reordered-decoded` gains a Discontinuity at its seam — the clearest single
statement of what this release means). Tiers: 32 accept, 12 isolate, 5 reject.
`tunnel/` is a four-file accept-tier fixture; our harness already handles
multi-file accept fixtures via `chain/`, so the new work is verifying each
declared `digest` against the sibling it names, which the manifest says is a real
SHA-256 throughout.

**`SPEC_VERSION`** moves to `0.15` and `0.14` files stop being readable, per the
`0.x` rule. `VECTOR-DEFECTS.md` gains an entry if Finding 2 is resolved against the
vector rather than the text.

---

## How this was reviewed

Read the `0.15` specification and CHANGELOG in full, diffed against the `0.14`
text section by section (969 diff lines), and read every new and changed vector —
`.jsonl` projections for the accept tier, annotated `.hex` for
`isolate-self-derived` and `undecoded-in-capture`, and the manifest's `summary`
and `expect` fields for all 49. Findings 1, 2 and 4 come from checking the prose
against the shipped bytes rather than from reading either alone; Finding 3 from
asking what enforces the release's own "the unit is the stream" claim; Finding 7
from asking which of the tunnel fixture's four files the rules would still work on
if a stage dropped something from it. Nothing was implemented — this is a
paper review against a working `0.14` implementation.

# Defects in the conformance vectors

**Two open, four closed.** Defects 5 and 6 are live against the vectors
currently vendored in [`tests/vectors/`](tests/vectors/), which are `v0.19`;
together they hold three cases out of the suite via `DEFECTIVE` in
[`tests/test_vectors.py`](tests/test_vectors.py). The other four were reported
upstream and fixed.

| | Defect | Found against | Status |
|---|---|---|---|
| 1 | Three decode-stage vectors omit `produced_by`/`produced_at` | `v0.12` | Fixed in `0.13` — CHANGELOG *Fixed*; all three now carry both options |
| 2 | `vectors/README.md` contradicts the spec on the `isolate` tier | `v0.12` | Fixed in `0.14` — the README now reads "Rejecting an `isolate` vector, with a diagnostic, **is conformant**" |
| 3 | `undecoded-in-capture` writes ids no reading of the text allows | `v0.15` | Fixed in `0.16` — CHANGELOG *Changed* ([#87](https://github.com/adamkjonsson/zipline/issues/87)); the vector's `session_id` `7` → `0` |
| 4 | `tunnel/{inner,outer}.jsonl` spell the flow key `flow_key`, not `key` | `v0.16` | Fixed in `0.17` ([#104](https://github.com/adamkjonsson/zipline/issues/104)), with a `check.py` guard building the key vocabulary from the spec's own tables |
| 5 | `mixed-derivation`'s identity span writes `pid` and `session_id` transposed | `v0.19` | **Open** — reported as [#141](https://github.com/adamkjonsson/zipline/issues/141) |
| 6 | `handshake-at-origin` and `unplaceable-below-origin` write `tcp_role` one below the value their `.jsonl` states | `v0.18`, `v0.19` | **Open** — reported as [#141](https://github.com/adamkjonsson/zipline/issues/141) |

**Defects 5 and 6 share a root**, which is why they were reported as one issue:
`build.py` authors the `.zpf` and the `.jsonl` faces of each vector
independently, so the two can disagree and nothing upstream notices. The `.hex`
is generated from the same description as the bytes, so it reproduces the wrong
value rather than contradicting it; `check.py` is barred from parsing block
bodies by the suite's own ground rule 2, which is exactly what comparing the
faces would need. So the agreement of a vector's two faces is unguarded by
construction, and an implementation reading the binary is the only thing that
tests it. That is now how three of the six defects here surfaced.

Defect 1 also generalised upstream. The principle this file drew out of it — **a
negative vector must carry exactly one violation** — is now stated in the vectors
README and *enforced*: every manifest entry declares a `violations` count, and
`check.py` requires it to agree with the tier. Declaring it is mandatory, so a
vector cannot be written without confronting the number.

Defects 1 and 2 were found during the 0.9 → 0.12 port, against `vectors/` at tag
`v0.12` (commit `c291afc`). Defect 3 came out of the `0.15` review, defect 4 out
of Phase 1 of the 0.14 → 0.16 port, and defects 5 and 6 out of Phase 0 of the
0.16 → 0.19 port. Each is recorded next; the frozen 0.12 report follows them.

---

## Defects 5 and 6 — three vectors whose `.zpf` contradicts their own `.jsonl`

**Open at `v0.19`.** Found at Phase 0 of the 0.16 → 0.19 port, by projecting
every vendored `.zpf` through our JSONL face and diffing against the shipped
`.jsonl`. Of the 40 files carrying both faces, three disagree, and in every case
the **binary** is the wrong half — which is the half an implementation is tested
against.

### What is wrong

| Vector | The `.zpf` holds | The `.jsonl` says | Wrong since |
|---|---|---|---|
| `mixed-derivation` | span `pid = 8, session_id = 0` | `session_id = 8, pid = 0` | `0.19` |
| `handshake-at-origin` | `tcp_role` 0 then 1 | `initiator` then `responder` — 1 then 2 | `0.18` |
| `unplaceable-below-origin` | `tcp_role` 0 | `initiator` — 1 | `0.19` |

Semantics settle which face is right in each case, and it is always the `.jsonl`.

`mixed-derivation`'s session 11 preserves a stream whose input is session 8,
participant 0; the sibling span in the same file cites `(source 1, pid 0,
session 7)` correctly, and a participant id of 8 in a file whose participants
are all pid 0 is not a reading anyone intended.

`handshake-at-origin`'s pid 0 sends the first SYN at `ts 1000` carrying no
`ack`, and pid 1 answers at `ts 1100` with `ack 1001`. So pid 0 is the initiator
and pid 1 the responder, which is what the `.jsonl` says and what the vector's
own summary describes. The bytes say *unknown* then *initiator*: the enum
shifted down by one. `unplaceable-below-origin` carries the same shift.

### The cause

`build.py` authors the two faces independently, so they can disagree:

```python
# handshake-at-origin, the binary
participant(7, 0, [o_endpoint("10.0.0.1:51000"), o_isn(1000), o_tcp_role(0)]),
participant(7, 1, [o_endpoint("93.184.216.34:80"), o_isn(5000), o_tcp_role(1)]),
# ... and, separately, its jsonl
"tcp_role": "initiator",
"tcp_role": "responder",
```

`o_tcp_role` takes the wire value, so those should be `1` and `2`.
`escape-unknown-enum` writes `o_tcp_role(7)` against `"tcp_role": 7` and is
correct, so the helper is not at fault.

The span case has a sharper proximate cause. `o_spans` takes its tuple in
**byte** order rather than logical order:

```python
u16(sr) + u16(pid) + u64(se) + ...   for sr, pid, se, a, b in entries
```

Every other statement of that triple — the specification's prose, the JSONL, the
`input_extents` entry — puts `session_id` before `pid`; the u16s lead only for
alignment. So `o_spans([(1, 8, 0, 0, 10)])` reads naturally as *session 8, pid 0*
and packs the opposite.

### Why nothing upstream caught it, and why that matters

The `.hex` is generated from the same description as the `.zpf`, so it
reproduces the wrong byte and annotates it faithfully:
`option 0x0063 tcp_role, len = 1  (0)`. `check.py` is barred from parsing block
bodies by ground rule 2, which is exactly what comparing the faces needs. The
capability check sees both option ids present and is satisfied; only their
*values* are wrong.

So the agreement of a vector's two faces is unguarded by construction, and the
only thing that tests it is an implementation reading the binary and projecting
it. Three of the six defects in this register surfaced that way.

### What we do about it

All three go into `DEFECTIVE` and are held out of the ratchet. Two of them carry
a lesson the defect does not touch, and neither is bent to fit:

- **`handshake-at-origin`** exists for the non-descending ordering MUST
  ([zipline#124](https://github.com/adamkjonsson/zipline/issues/124)), and that
  lesson is intact — the `seq_start` tie is in the bytes and correct. Only the
  projection is wrong.
- **`unplaceable-below-origin`**'s extent lesson is intact too, `tcp_role` having
  nothing to do with placement. We assert its extent through `_ACCEPT_EXTENTS`,
  which reads the `.zpf` and not the projection, so the defect does not cost us
  the guard that vector was added for.

---

## Defect 4 — `tunnel/inner.jsonl` and `tunnel/outer.jsonl` use the binary field name for the flow key

**Closed in `0.17`.** Found at Phase 1 of the 0.14 → 0.16 port, when the version
gate moved and `tunnel/outer` became readable for the first time. Both files
spell the key `"key"` at `v0.19`, and
[zipline#104](https://github.com/adamkjonsson/zipline/issues/104) added a
`check.py` guard that builds the projection's key vocabulary from the
specification's own tables — the part that outlives the fix, since it closes the
class rather than the instance. The report below is kept as written.

### What is wrong

Both files spell the Session flow key `"flow_key"`:

```jsonl
{"type":"session","session_id":1,"proto":"udp","flow_key":"198.51.100.7:51820 -> 203.0.113.9:51820"}
```

The JSONL ↔ binary mapping lists it among the **brevity aliases** — the keys
whose JSON name deliberately differs from the binary name — as `key`:

| JSONL key | Binary field / option |
|---|---|
| `key` | `flow_key` (Session) |

and adds, immediately below, "(`proto` is **not** an alias — its JSON key equals
its option name.)", which is the sentence that makes the intent unmistakable.

### The suite disagrees with itself

`descriptive-metadata.jsonl`, in the same vendored tree, writes `"key"`. So do
the specification's own older examples. The two spellings are both present at
`v0.16`:

| File | Spelling |
|---|---|
| `descriptive-metadata.jsonl` | `"key"` ✓ |
| `tunnel/inner.jsonl` | `"flow_key"` ✗ |
| `tunnel/outer.jsonl` | `"flow_key"` ✗ |

The specification carries the same split: `"key"` at the IRC example, and
`"flow_key"` in the *Worked example: a decrypted tunnel* walkthrough that the
`tunnel/` fixture accompanies. Both the walkthrough and the fixture are new in
`0.15`, and both were evidently written from the option registry rather than
from the alias table.

### Why it matters

It is small and it is not cosmetic. A reader that follows the mapping table —
as this implementation does — emits `"key"` and cannot round-trip either file,
so two of the four members of the suite's largest fixture fail the accept tier
for a reason that has nothing to do with what `tunnel/` exists to test. A reader
that follows the fixture instead writes a JSONL key no other vector uses.

It is vector-side under the vectors' own ground rule 2 — the normative mapping
is unambiguous, and one vector already obeys it — so the fix is to the two
`.jsonl` files and the walkthrough, not to the table.

### What we do meanwhile

`tunnel/inner` and `tunnel/outer` are listed in `DEFECTIVE`, which exists to keep
exactly this apart from the not-yet-ported xfails: nothing here bends the
implementation to match a broken fixture, and if either starts passing, either
the vector was fixed or we got the mapping wrong.

Reported at Phase 8 as
[zipline#104](https://github.com/adamkjonsson/zipline/issues/104), with the rest
of the port's findings — see the register in
[`SPEC-0.16-MIGRATION-PLAN.md`](plans/SPEC-0.16-MIGRATION-PLAN.md#phase-8--prose-and-the-upstream-report).
Batching was deliberate: a defect reported after the rule is implemented and the
vectors pass is a report backed by a working reader rather than a guess about
what the text means. That paid off here — the issue could say which two of the
suite's 53 vectors are affected, that every other one passes, and that one
vector in the same tree already obeys the rule. This entry stays the record
either way; the issue is the report.

---

## Defect 3 — `undecoded-in-capture` could not be parsed from the `0.15` text

**The one defect so far that was not vector-side alone**, which is why it is
recorded here rather than only in [`SPEC-0.15-REVIEW.md`](plans/SPEC-0.15-REVIEW.md)
(Finding 2). Every other entry in this file rests on the vectors' own ground rule
2 — a vector that disagrees with the specification is the thing that is wrong.
Here the specification disagreed with *itself*, so no reading could have produced
a correct vector, and the fix had to settle the text before it could touch the
bytes.

### What was wrong

The block that shipped at `v0.15`:

```jsonl
{"type":"undecoded","source_id":1,"session_id":7,"pid":0,
 "off_start":4096,"off_end":4396,
 "reason":"overlap-discarded","reason_class":"bytes"}
```

`source_id 1` is a `capture` Source. Three normative statements bore on it and no
reading satisfied all three:

| Statement | Where | What it required here |
|---|---|---|
| `source_id` is the input Source **`kind = zpf-input`** | Undecoded field table | the block is illegal |
| the ids are the *source's*, **never the current file's** | §Undecoded | a capture has no id namespace, and `7` **was** this file's session id |
| for a `capture` source the ids are **unused (write 0)** | span-list rule | `session_id` must be `0`, not `7` |

The file's own `.hex` annotated those ids as "in the input's namespace" for ids
that were demonstrably this file's.

### How it was resolved

**The span-list rule won**, so an Undecoded body and a `spans` entry are now read
by one rule as well as parsed by one struct. Against a `capture` source the ids
are unused and MUST be written `0`, and the offsets are byte offsets into the
capture file.

That is *not* the reading this project recommended. The review argued for the
stream-offset reading, on the grounds that a capture-file byte offset cannot name
a segment that was never captured, which would make the `hole` class unreachable
there. `0.16` accepted the consequence and stated it outright: against a
`capture` source only the bytes-exist class is available, because a hole in a
reassembled transport stream is already carried by its own hole-inclusive
offsets, and declaring it twice is the contradiction that also bars a
Discontinuity from such a stream. That is a better answer than the one proposed —
it removes the second account rather than relocating it — and
`isolate-hole-against-capture` now tests the bar.

### Why it never reached this tree

We vendored `v0.14` and then `v0.16`, never `v0.15`, so `undecoded-in-capture`
arrives here already carrying `session_id = 0`. It is catalogued because the
defect is the reason [`SPEC-0.15-REVIEW.md`](plans/SPEC-0.15-REVIEW.md) called `0.15`
unimplementable-as-written, and because it is the precedent for what to do when
a vector and the text cannot both be right: report it, and do not guess.

---

The rest of this file is as it was filed, against `v0.12`.

---

Nothing here challenges the *normative text*. Per the vectors' own ground rule
2, a vector that disagrees with the specification is the thing that is wrong,
and all of these are vector-side.

---

## Defect 1 — Three decode-stage vectors omit the mandatory `produced_by`/`produced_at`

**Affected:**

| Vector | Tier | Consequence |
|--------|------|-------------|
| `undecoded-skipped` | `accept` | Fails a **correct** reader |
| `undecoded-reason-class` | `accept` | Fails a **correct** reader |
| `isolate-coverage-gap` | `isolate` | Passes an **incorrect** reader |

### What is wrong

All three are decode-stage files: each declares a `zpf-input` Source, declares a
Decoder Descriptor, and carries records with `decoder_id`/`spans` or Undecoded
blocks. Each therefore falls under *Conformance*:

> Every **derived** file (either kind) MUST declare each of its input `.zpf`s as
> a `zpf-input` Source and set the File Header `produced_by`/`produced_at`.

None of the three sets either field. Their File Headers carry no options at all
(`length = 16` — magic, version, `tick_hz`, nothing more). The `.jsonl`
projections agree with the bytes, so the two faces are consistent with each
other; both are simply non-conformant.

### Evidence

Walking each file's blocks:

```
undecoded-skipped        FileHeader produced_by=None produced_at=None
                         Source id=1 kind=zpf-input uri='raw.zpf'
                         Decoder / Session / Participant
                         Undecoded reason='skipped' decoder_id=1
                         Record decoder_id=1 spans=1

undecoded-reason-class   FileHeader produced_by=None produced_at=None
                         Source id=1 kind=zpf-input uri='raw.zpf'
                         Decoder / Session / Participant
                         Undecoded reason='rtp-seq-gap' decoder_id=1

isolate-coverage-gap     FileHeader produced_by=None produced_at=None
                         Source id=1 kind=zpf-input uri='raw.zpf'
                         Decoder / Session / Participant
                         Record decoder_id=1 spans=1
                         Undecoded reason='undecodable' decoder_id=1
```

For contrast, every *other* derived vector — `decoded-basic`,
`annotator-decoded`, `passthrough-transport`, `reordered-decoded`, and all three
`chain/` files — sets both fields correctly. The omission looks like an
oversight in three files rather than a misreading of the rule.

**Everything else about them is right.** Having implemented `reason_class` and
the `skipped` reason, we projected both `accept` vectors and compared against
their shipped `.jsonl`: the output matches, line for line, including
`undecoded-reason-class`'s non-canonical `rtp-seq-gap` reason carrying
`reason_class: "hole"`. So these two vectors test exactly what they set out to,
and adding the two header options is the whole fix — no other content needs
revisiting.

### Why it matters

The two tiers fail in opposite and equally unhelpful directions.

**The two `accept` vectors punish a conformant reader.** A reader that
implements the derived-file rule MUST diagnose these files, which is precisely
what the `accept` tier forbids. The more correct the implementation, the more
certainly it fails.

**`isolate-coverage-gap` rewards a non-conformant one.** It carries two
violations: the coverage gap it exists to test, and this missing provenance. A
reader hits the provenance error first, isolates the file, and passes the vector
**with the coverage check entirely unimplemented** — which is exactly what this
implementation did. That defeats the vector's purpose, and the coverage
guarantee is the format's central honesty claim, so this is the one negative
vector it is least affordable to have inert.

The general principle is worth stating because it will recur: **a negative
vector must carry exactly one violation.** With two, it silently tests whichever
the reader happens to detect first.

### Suggested fix

Add `produced_by` and `produced_at` to the File Header of all three, matching
what `decoded-basic` already does, and regenerate the `.hex` and `.jsonl`
alongside. `isolate-coverage-gap` then carries only the coverage gap, and the
two `accept` vectors become conformant files.

---

## Defect 2 — `vectors/README.md` contradicts the specification on the `isolate` tier

### What is wrong

The README says both of these:

> **Tier table:** `isolate` — Well-framed but semantically invalid. A reader MAY
> reject the file *or* discard the smallest unit it can soundly isolate — and
> MUST NOT silently repair, guess, or drop without a diagnostic.

> **Ground rule 4:** A reader that *rejects* an `isolate` vector is as wrong as
> one that accepts it silently.

The table permits rejection; the prose calls it as wrong as silent acceptance.

The specification is unambiguous and matches the table — *Conformance*, semantic
violations: "the reader MAY reject the file, or discard the smallest unit it can
soundly isolate". So the prose is the error.

### Why it matters

It changes what a harness asserts, and it is stated in the section explaining
why the negative vectors are the valuable half — so it is the sentence an
implementer is most likely to encode. A harness built on the prose will fail a
reader that legitimately rejects.

Our harness follows the specification: it accepts either outcome and asserts
only that the violation is not passed silently.

### Suggested fix

Reword ground rule 4 to match the table and the spec — the failure mode being
warned against is a reader that *accepts silently*, and one that treats a
semantic violation as structural corruption (rejecting for the wrong tier's
reason). Rejecting the file, with a diagnostic, is conformant.

---

## Verified sound

Checked while investigating, and correct — recorded so the batch report does not
imply doubt about them:

- **`raw-minimal` is byte-for-byte the specification's 196-byte worked example.**
  Independently confirmed: this project transcribed the same example into a test
  fixture by hand, and after the 0.12 version stamp the two agree exactly.
- **`undecoded-reason-class` carries `reason_class = "hole"`** (option `0x00A1`)
  on its non-canonical `rtp-seq-gap` reason, exactly as required. Only its
  File Header provenance is wrong.
- **All other derived vectors set `produced_by`/`produced_at`.**

## How the audit was run

Defect 1 was found by tripping over `isolate-coverage-gap`, then generalised:
for every non-`reject` vector, walk its blocks, classify it as derived if it
declares a `zpf-input` Source or carries `decoder_id`/`spans`/`origin`, and flag
any derived file whose File Header lacks `produced_by` or `produced_at`. That
swept up the two `accept` vectors, which had not been reached yet by the port.

The same shape of check is worth running upstream over `build.py`'s
descriptions, since it is mechanical: a vector's declared tier and its actual
violation count should agree, and a negative vector should carry exactly one.

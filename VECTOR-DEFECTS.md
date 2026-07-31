# Defects in the 0.12 conformance vectors

A running list, to be reported to the
[zipline](https://github.com/adamkjonsson/zipline) project in one batch when the
0.9 → 0.12 port is finished. Found while implementing against
`vectors/` at tag `v0.12` (commit `c291afc`).

**Status: 2 defects, affecting 3 vectors and the vectors README.** Appended to as
later migration phases exercise more of the tree — the `accept` tier's
projections are not fully compared until Phase 2, so this list is not yet
complete.

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

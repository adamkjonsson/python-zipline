# Zipline Payload Format 0.13 — review

Review of the [0.13 specification](https://github.com/adamkjonsson/zipline/blob/v0.13/docs/zipline-payload-format.md)
and [CHANGELOG](https://github.com/adamkjonsson/zipline/blob/v0.13/CHANGELOG.md),
tag `v0.13`. Written from `python-zipline`, a complete implementation of `0.12`
(binary container, JSON-Lines projection, conformance checker, causal merge,
decode-stage / pass-through / filter transforms, provenance walk).

Third round, and the first reviewing a release that *adds* rather than only
corrects. Findings from the `0.10`–`0.12` rounds were all adopted; this one is
written the same way — nothing here challenges the normative model, and the
suggested fixes are all local.

---

## Verdict

**This is the strongest release since `0.9`, and the Discontinuity block is the
reason.** Finding a live silent-corruption path on the specification's own
flagship chain — `raw → tls-records → http`, no tunnel, coverage passing, a
consumer reading a message that was never sent — is exactly the class of defect a
format wants found before `1.0` rather than after. The analysis of *why* it needs
its own block, why it cannot be an Undecoded variant or a record option, and why
an absent `width` must contribute `0` rather than poisoning the rest of the
stream, is all correct and unusually well argued.

The honesty is also worth noting: three entries in this release exist because the
specification's own text, or its own vectors, were found to be wrong. A format
that reports that on itself is one implementers can trust.

**Seven findings, none fatal, four worth settling before anyone implements.** One
produces wrong numbers, one produces dangling references, one means the release's
flagship block can be fully implemented and still not prevent the corruption it
exists to prevent, and one is a capability shipped with no vector whose obvious
implementation is wrong.

Three of those four share a cause, and it is worth naming up front: **`0.13` adds
two features that interact — `width` in the positional arithmetic and
`input_extents` over fan-out — and the places where they meet are the places the
text disagrees with itself or the suite says nothing.** Individually each feature
is well specified; it is the seams that need a pass.

---

## Findings

### 1. The decoded offset space is now defined three times, and only one definition includes declared widths

**Severity: high — silently wrong offsets and extents.**

`0.13` adds `width` as a term in the positional arithmetic, but only updates one
of the three places that define what a decoded stream's offset space *is*:

| Where | What it says | Widths? |
|---|---|---|
| *Layers* — "Each layer has its own offset space" | "the **concatenation of that participant's decoded record payloads in stored order**" … "unlike a transport stream's, is **not** hole-inclusive" | ✗ |
| *Layers* — the positional rule | `[ Σ(preceding payload_len + preceding declared widths), + its own payload_len )` | ✓ |
| *Session End* — `input_extents` | "a decoded stream's is the concatenation of its record payloads" | ✗ |

All three were correct in `0.12`. After `0.13` they disagree, and the two that
were not updated are the *defining* statement and the *extent* rule — the two a
reader is most likely to implement from.

The consequences are concrete. On any file carrying a known-width Discontinuity:

- a consumer following the offset-space definition computes every later record's
  range short by `Σ width`, while one following the positional rule does not —
  and the `discontinuity-known-width` vector exists precisely because those two
  answers differ visibly (`[75,105)` versus `[50,80)`);
- a checker comparing a declared `input_extents` value against the union of
  covering spans fails systematically, because the extent gloss and the spans it
  is compared against are computed under different rules.

The "**not** hole-inclusive" clause is also now false as stated: a known-width
Discontinuity makes a decoded stream hole-inclusive in exactly that region. That
sentence carries weight — it is the one-line contrast people remember — so
leaving it absolute will propagate the error.

**Suggested fix.** Make the *Layers* definition the single normative statement,
including declared widths, and reduce the other two to references. Something
like: *a decoded stream's offset space is the concatenation of that participant's
record payloads in stored order, plus the declared `width` of any Discontinuity
between them; it is hole-inclusive only where a Discontinuity declares a width.*
Then have `input_extents` say "in that stream's own offset space (see Layers)"
without re-glossing it.

### 2. The pass-through rule for Discontinuity is stated two contradictory ways

**Severity: high — produces dangling ids.**

> *§Discontinuity (`0x22`)*: "A pass-through preserving a **decoded** layer MUST
> re-emit its input's Discontinuity blocks **unchanged, exactly as it re-emits
> Undecoded blocks**"

> *§Conformance*: "**But it re-emits these differently from Undecoded blocks, and
> the difference is the point:** … A Discontinuity's ids name the stream in the
> file that carries it, so a pass-through **renumbers** them to its own
> `session_id`/`participant_id`"

These are opposites, and each invokes the comparison to Undecoded to make its
point. The Conformance passage is the correct one — it matches the CHANGELOG and
follows from the block's defining property — so the error is in the block's own
section, which is the first place an implementer looks.

Following the letter of §Discontinuity produces a file whose Discontinuity blocks
name session/participant ids from the *input's* namespace while every other id in
the file is the pass-through's own. Where a pass-through mints fresh ids, those
references dangle.

**Why it survived:** nothing tests it. `annotator-decoded` is the suite's
decoded-layer pass-through and carries an inherited *Undecoded* block only. No
vector combines a pass-through with a Discontinuity — which is the same shape of
gap as the `isolate-coverage-gap` lesson this release just closed on the negative
tier.

**Suggested fix.** Correct §Discontinuity to state renumbering, and add a vector:
a decoded-layer pass-through carrying an inherited Discontinuity, with the ids
visibly renumbered. That vector would also have caught this before release.

### 3. The block's central duty is described but never required

**Severity: medium — the block can be fully implemented and still not work.**

Everything the specification says about *not joining across* a Discontinuity is
descriptive:

> It asserts that the two sides **do not join**, which is the actual defect: a
> consumer that splices them reads a message that was never sent.

There is no MUST anywhere. That is out of character for this document, which is
otherwise precise about reader duties — "a reader **MUST NOT** guess a class", "a
reader **MUST NOT** re-sort a SEQUENCED session", "a reader **MUST NOT** assume a
`bytes` option is text".

As written, a downstream decode stage may read a Discontinuity, skip nothing,
compute every offset correctly, emit a unit whose `spans` cross the break, pass
coverage — and remain conformant. The block would be present in the file and
inert in practice, which leaves the original finding only half closed: the
information is now *recorded*, but nothing obliges anyone to *act* on it.

**Suggested fix.** Two sentences in §Discontinuity, in the document's usual
register:

> A consumer **MUST NOT** treat the records either side of a Discontinuity as
> contiguous. A decode stage reading an input that carries one **MUST NOT** emit
> a unit whose `spans` cross it without emitting a Discontinuity of its own in
> the corresponding position of its output.

The second half is what carries the property down a chain — otherwise the break
is visible at stage 2 and gone by stage 3.

### 4. Session fan-out ships with no vector at all, and its checker rule is the subtle one

**Severity: high — a natural implementation passes every vector and is wrong.**

Fan-out is one of the release's two *Clarified* items, and unlike the other it is
exercised by nothing. Across all 32 vectors, every decode stage's output sessions
mirror its input's one-to-one:

- `decoded-basic` — one session (`7`), two participants, output ids equal to the
  input's;
- `isolate-extent-exceeds-coverage` — one session (`7`), one participant;
- `chain/decoded`, `undecoded-*`, `discontinuity-*`, `reordered-decoded` — all
  single-session, all mirroring.

Nothing demultiplexes one input stream into several output sessions, nothing
draws one output session from several input streams, and nothing mints a session
with no upstream counterpart. (By contrast the *other* clarification,
correspondence-not-identity, is exercised — `chain/decoded.zpf` has emitted an
8-byte payload spanning 16 since `0.11`, as the release itself notes.)

That gap matters more than an untested permission usually would, because fan-out
and the new `input_extents` interact, and the interaction is where a reader can
be quietly wrong:

> **Under fan-out, every consuming session declares the same full extent.** … A
> checker therefore unions the covering spans across *all* sessions naming the
> stream and compares that union against the one extent.

The obvious implementation — accumulate coverage per output session, compare each
session's spans against the extent that session declared — passes all 32 vectors,
because in every one of them the two formulations coincide. It then fails on the
first HTTP/2 file it meets, reporting a coverage gap in each session for the
bytes the *other* sessions covered. Our own checker is currently keyed on the
input stream rather than the output session, so it would survive; that is luck,
not design, and the vectors would not have told us either way.

The same applies to the contradiction rule — "two sessions declaring **different**
extents for one stream … a reader MAY treat as a semantic violation" — which has
no vector because there is no multi-session vector to build it from.

**Suggested fix.** Two vectors, and the first is the more important:

- an `accept` vector demultiplexing one input participant stream into two output
  sessions, each declaring that stream's **whole** extent in `input_extents`,
  with their covering spans partitioning it — so a per-session checker fails and
  a per-stream one passes;
- an `isolate` vector where two sessions declare **different** extents for one
  stream.

The first would also be the suite's first file exercising an output session with
no one-to-one input counterpart, which is worth having regardless of extents.

### 5. `input_extents` is optional, with no SHOULD, so self-verifiability is opt-in twice over

**Severity: medium.**

The option is the answer to a real gap and its design is right — Session End is
the correct home, and the fan-out rule (every consuming session declares the
stream's *whole* extent, checker unions across them) is exactly right.

But Session End is `SHOULD`, `input_extents` is neither `MUST` nor `SHOULD`, and
the only guidance is "a writer that does not know a stream's extent omits the
entry". So a consumer meeting a file with no extents cannot distinguish:

- the writer did not know,
- the writer did not bother,
- the writer predates `0.13`.

All three look identical, and the third will be the common case for a long time.
The property the option exists to give — *detecting a stage that stopped early
and said nothing* — is therefore only obtainable from writers that opt in, which
are not the writers that silently truncate.

This does not make the option wrong; it makes its stated benefit conditional in a
way the text does not say. **Suggested fix:** a `SHOULD` for the case that
matters — *a decode stage that knows an input stream's extent SHOULD declare it*
— and one sentence telling a consumer that an absent entry asserts nothing.

### 6. "Accounted for exactly once" and "belongs when it fed" pull in different directions

**Severity: medium — clarity, on a rule this release deliberately loosened.**

The correspondence clarification is right, and the argument against the identity
reading (deflate is stateful; the alternative is O(n) spans per record) is
convincing. But the same paragraph states two rules that do not sit together for
exactly the decoders it invokes to justify itself:

- *the rule*: "a region belongs in a unit's span set when it **fed** that unit";
- *the guarantee*: "every input offset is still accounted for **exactly once**,
  by a span or by an Undecoded block".

Under deflate or HPACK, an early region feeds *every* later unit. Applying the
first rule literally puts it in many span sets, which contradicts "exactly once"
— and is the O(n) explosion the paragraph has just rejected. So the workable rule
is narrower than "fed": each input region is cited by **one** output unit, the
one whose emission it completed. That is what an implementer will do; it is
simply not what the text says.

Related and unsettled: the coverage guarantee says "never both" of *span versus
Undecoded*, and says nothing about the same range appearing in **two records'**
spans. "Exactly once" implies that is a violation. A checker needs to know
whether to flag it — ours currently does not, and would need to if it is.

**Suggested fix.** State the narrow rule directly ("cited by exactly one output
unit — the one whose emission it completed"), and say explicitly whether
span-on-span overlap within one input stream is a semantic violation.

### 7. `input_extents` does not state its entry size the way `spans` does

**Severity: low — consistency.**

`spans` is pinned down: "a **span-list** value is `count` packed entries, each 28
bytes … (`count = len / 28`)". `input_extents` gives the field layout but never
the entry size or how a parser derives the count. It is 20 bytes and derivable,
but the asymmetry is the kind that invites an off-by-one in someone's parser.

Worth one clause, for symmetry with the sibling packed type.

---

## Verified sound

Checked while reviewing, and correct — recorded so the report does not imply
doubt about them:

- **Packed layouts.** An `input_extents` entry is 20 bytes (`2+2+8+8`) and a
  Discontinuity body 12 (`8+2+2`); both are multiples of 4, and both lead with
  the u16s so the u64s stay 4-byte aligned, consistent with `spans` and `origin`.
- **The `bytes` option type is properly closed off.** `external_session_id`
  projects as base64 under the existing raw-bytes rule, and the explicit "a
  reader MUST NOT assume a `bytes` option is text, even when it decodes to
  printable ASCII" forecloses the obvious mistake.
- **`discontinuity` is in the JSONL `type` table**, so the new block has a
  projection rather than falling through the unknown-block escape.
- **The "not safe to skip" analysis is right**, including the consequence that
  such a block needs a *major* bump after `1.0`, and the reframing of the
  minor-bump test as "is a reader that skips this still correct" rather than "can
  a reader skip this". That reframing is worth more than the block itself.
- **The version re-stamp removal** is well reasoned — unnecessary within `1.x`,
  insufficient across a major, and a transcoding specification in disguise. The
  disposability position now living in the specification rather than only in the
  CHANGELOG's *Conventions* is the right home; we hit exactly that gap porting
  `0.9 → 0.12`.
- **The `violations` count in `manifest.json`**, declared rather than computed,
  with `check.py` deliberately not a conformant reader. That is the correct call:
  a checker that ruled on semantics would become a second normative authority.

---

## Implementation impact

Included as evidence rather than a request. `python-zipline` implements `0.12`;
moving to `0.13` looks like a small job with one exception.

- **`external_session_id`, `transform_params_digest`** — new options, mechanical.
  The first `bytes`-typed option needs a codec, but the projection rule is
  already ours.
- **`input_extents`** — a new packed repeatable option, structurally identical to
  `spans`; and a real addition to our coverage checker, which currently *requires*
  the input file to compute extents. This is what lets it check a file alone.
- **Session fan-out** — our coverage checker is keyed on the *input stream*
  rather than the output session, so it already unions across sessions and should
  survive. That is luck rather than design (finding 4), and with no vector to
  confirm it, verifying it means writing our own.
- **Correspondence, not identity** — nothing to change: we never asserted
  payload-to-span identity. Worth a test that a record shorter than its span is
  accepted.
- **`Discontinuity`** — the real work, and it touches the one thing Phase 6 built:
  positional ranges. Our `record_ranges()` computes the decoded space as a payload
  concatenation, which is where finding 1 lands directly. Resolving finding 1
  decides whether that function sums widths, and resolving finding 3 decides
  whether our reader owes a duty at the join.

Nothing here changes a file we have already written, which matches the release's
own claim that no `0.12`-conformant file stops being conformant.

---

## How this was reviewed

Diffing `v0.12` against `v0.13` (441 added lines), reading the new block and
option definitions in full, cross-checking every restatement of a rule the
release touched, and walking all 32 vectors in the shipped `manifest.json` —
parsing several at the frame level, since a `0.12` reader correctly refuses to
open them.

Two cheap checks would have caught four of the seven findings between them, and
both are mechanical enough to belong on the release checklist:

- **For each rule this release touches, grep for every place that states it.**
  Findings 1 and 7 are rules restated in several places with only some copies
  updated; finding 2 is the same fault where the two copies actively contradict.
- **For each capability this release adds or clarifies, name the vector that
  exercises it.** Findings 2 and 4 are untested paths — a pass-through carrying a
  Discontinuity, and any output session not mirroring its input. This release
  already added `violations` to `manifest.json` to make a vector's *tier* honest;
  the same instinct applied to coverage of the feature list would have shown that
  one of the two *Clarified* items shipped with nothing exercising it.

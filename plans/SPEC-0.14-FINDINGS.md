# Findings against the 0.14 specification

Found while porting `python-zipline` from `0.12` to `0.14` (see
[SPEC-0.14-MIGRATION-PLAN.md](SPEC-0.14-MIGRATION-PLAN.md)). This is the record
of what was reported upstream and what is still to report; the argument for each
lives here and nowhere else, so there is one copy to correct if it turns out to
be wrong.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | A decode stage that *creates* a break in its own output owes nothing | high | **Registered — [zipline#78](https://github.com/adamkjonsson/zipline/issues/78)** |
| 2 | §File Header's field table still says `version_minor` is `13` | low | Not yet filed |

---

## 1 — A decode stage that *creates* a break in its own output owes nothing

**Registered as [zipline#78](https://github.com/adamkjonsson/zipline/issues/78).**
Text as filed:

> `0.13` added the Discontinuity block and `0.14` gave it teeth. Both are aimed at
> one thing: a consumer that splices two records which do not join reads a message
> that was never sent. But every duty the specification attaches to the block is
> about **propagating a break the input already declared**. Nothing covers a stage
> that *creates* one, and two ordinary stage kinds — a filter and a reorder — do
> exactly that. The specification names both, blesses both, and ships a vector for
> one.
>
> ### The rules, and what they are conditioned on
>
> There are four normative statements about emitting the block, and every one of
> them presupposes an input that already carries it:
>
> > §Discontinuity — *What a consumer owes the block*: A consumer **MUST NOT**
> > treat the records either side of a Discontinuity as contiguous. A decode stage
> > reading an input **that carries one** MUST NOT emit a unit whose `spans` cross
> > it without emitting a Discontinuity of its own in the corresponding position of
> > its output.
>
> > §Discontinuity — *pass-through*: Such a transform **MUST re-emit every
> > Discontinuity in its input**, in its position in the participant's stored order
> > and with its `width` unchanged.
>
> > §Conformance: It **MUST also carry every Discontinuity block forward**,
> > renumbered to its own ids.
>
> The first is propagation ("reading an input that carries one"); the second and
> third are the pass-through's copy duty. `grep -n 'Discontinuity' | grep -i 'must'`
> returns those and nothing else. **There is no rule that a stage whose own output
> breaks must say so.** §Discontinuity opens by defining what the block *means* —
> "Marks a break in **this** file's own output stream" — and never turns that into
> a duty to emit it.
>
> ### Case 1 — a reorder, which is a live `accept` vector
>
> `vectors/reordered-decoded` is the sharpest form, because nothing is dropped and
> coverage is perfect. Its two records, in stored order:
>
> | Stored | Payload | `spans` (input) | Positional range (output) |
> |---|---|---|---|
> | 1st | 50 × `B` | `[100,150)` | `[0,50)` |
> | 2nd | 100 × `A` | `[0,100)` | `[50,150)` |
>
> In the output offset space `[0,50)` and `[50,150)` are adjacent — byte 49 abuts
> byte 50. Upstream, byte 49 is input offset 149 and byte 50 is input offset 0.
> **They do not join; they are in reverse order.** The file contains no `0x21` and
> no `0x22`, the input stream `[0,150)` is covered exactly once, `violations` is 0,
> and the tier is `accept`.
>
> Now put a stage 3 on it. An HTTP decoder reading this file emits one unit
> spanning `[40,60)` of it — straight across the join — splicing the tail of the
> response's second half onto the head of its first. Coverage passes. Nothing in
> the file it is reading says the two sides do not join. That is, verbatim, the
> failure the `raw → tls-records → http` analysis in §Discontinuity was written
> about:
>
> > two records either side of an input gap are *adjacent* in it — the gap does not
> > survive the layer … coverage would pass … a consumer would read a message that
> > was never sent, with no marker in the file it was reading.
>
> **This is not a vector defect.** Under the current text `reordered-decoded`
> breaks no rule, and that is precisely the problem — the conformant output of a
> blessed transform is a file whose offset space lies about its own continuity.
>
> ### Case 2 — a filter, where the trace exists but is positionally mute
>
> §Layers requires a filter to mark every dropped region Undecoded with
> `reason = skipped`, so unlike the reorder there *is* something in the file. It
> does not help, for the reason §Discontinuity itself gives for why the two blocks
> cannot be merged:
>
> > Every field of an Undecoded block is read against the *input* … A Discontinuity
> > says "something is missing **here**, in what I produced", and its ids are this
> > file's own.
>
> An Undecoded block says bytes went uncarried **over there**. It cannot say where
> the output breaks, because Undecoded regions "contribute nothing" to the output
> offset space by §Layers' own definition. So a downstream stage computing
> positional ranges over the filtered file sees one contiguous stream, and splices
> across the drop exactly as in case 1.
>
> ### Why a consumer cannot recover this from `spans`
>
> The obvious objection is that a consumer could compare consecutive records'
> spans and notice they do not abut. Since `0.13` that inference is unsound:
>
> > **What `spans` asserts is correspondence, not identity.** … A record of 8 bytes
> > may span 16, or 16 000.
>
> Non-abutting spans no longer imply a break — a transforming decoder's spans need
> not abut even when its output is perfectly continuous — so the signal a consumer
> would have to read is one the format has deliberately stopped carrying. This is a
> seam between the release's two headline features rather than a flaw in either.
>
> ### Suggested fix
>
> State the duty in terms of the stage's **own output**, which is what the block is
> already defined to be about. Something in the document's register:
>
> > A decode stage **MUST** emit a Discontinuity between two records of its output
> > wherever their adjacency in its offset space is not an adjacency of the content
> > upstream — because it withheld an output unit between them, or because it
> > reordered them.
>
> Propagation then falls out as the special case it is, rather than being the only
> statement.
>
> Two details the wording has to get right, and I do not think either is settled:
>
> 1. **The duty must key on withheld output *units*, not unspanned input bytes.**
>    `reason = skipped` currently does two unrelated jobs: "I did not interpret
>    these bytes and nothing is missing" (a discarded byte-order mark — the case
>    §Undecoded introduces it for) and "I deliberately did not carry this record
>    forward" (a filter's dropped record). Only the second breaks the output
>    stream. A rule phrased as "any input region not covered by a span" would force
>    a Discontinuity after every skipped BOM, which is noise and would devalue the
>    block.
> 2. **How heavy is a reorder's obligation?** Taken literally, a thoroughly
>    reordered participant needs a Discontinuity at nearly every record boundary.
>    That is honest and is the same order as the records themselves, but it is
>    worth saying out loud rather than discovering — and it may argue for a
>    session- or participant-level assertion instead, of the "this stream's stored
>    order is not its content order" kind.
>
> ### Vectors
>
> Two, matching the release checklist's "name the vector that exercises it":
>
> - a **filter** that drops an interior decoded record, emitting the required
>   `skipped` Undecoded block *and* a Discontinuity at the join — currently no
>   vector shows a filter at all;
> - `reordered-decoded` revisited under whatever the rule becomes. It is the file
>   that demonstrates the gap, so it should be the file that demonstrates the fix.
>
> The `splice/` fixture is the model for the pairwise form: the violation here is
> likewise invisible in any single file, since the filtered or reordered stage is
> individually well-framed and fully covering, and the corruption only appears when
> a stage 3 reads it.

### What it blocks here

[D5](SPEC-0.14-MIGRATION-PLAN.md#d5--what-rewrite_decoded-owes-at-a-drop-point--needs-a-call)
of the migration plan. `rewrite_decoded()` implements both shapes — filter and
reorder — in one function, and the port has to decide what it owes at a drop
point. Until #78 resolves, the plan's recommendation is to emit a Discontinuity
behind a `mark_gaps: bool = True` parameter and to say in the docstring that this
**exceeds** what `0.14` requires.

---

## 2 — §File Header's field table still says `version_minor` is `13`

**Not yet filed.** Low severity, but worth batching with anything else `0.14`
turns up.

> \| `version_minor` \| u16 \| `13` for this document \|

Everything else in the release says `14`: the document title, the worked hex dump
(`000E  0E 00  version_minor = 14`), the CHANGELOG, and every shipped vector —
`raw-minimal` stamps `0E`, and `reject-unknown-minor` stamps `15`, which is
derived from the current version precisely so it keeps testing an unimplemented
minor. The table is a stale copy from `0.13`, and it is the same fault the release
exists to fix: a rule stated in more than one place with only some copies updated.

No ambiguity in practice — **we implement `14`** — but the field table is where a
reader author looks first, and a reader built from it would reject the entire
vector suite.

# Exposing a Record option

Where a new format option goes in the writer API, and why the answer is not
"another keyword". Written for
[#59](https://github.com/adamkjonsson/python-zipline/issues/59), which asked
for the policy as much as for the restructure.

## The rule

**Every Record option gets a keyword or a bundle.** A producer must be able to
write any option the format defines without dropping to the block layer — that
is what "support for the standard should be complete" means on the write side.

**`extra_options` is the only hatch**, and it is for options this library does
not know: an id from a future minor, or a private one. Reaching for it to write
an option we *do* know is a bug in this API, not a workaround.

**Where a new option lands is decided by the format, not by the count:**

| The option is… | It joins… |
| --- | --- |
| a transport-layer ordering hint | {class}`~zpf.Hints` |
| a decoded-layer statement | {class}`~zpf.Decoded` |
| timing | a flat keyword (`ts`, `ts_first`) |
| identity, framing, or free text | a flat keyword (`sender`, `source`, `flags`, `comment`) |

## Why those two bundles and no others

The test a grouping has to pass is the issue's own: **it earns its place only
if it names something a producer actually thinks in.** A bag of leftovers fails
that however much it shortens a signature, which is why there is no `Wire` or
`Meta`.

Two groupings pass, and both were named by the format before they were named
here:

- {class}`~zpf.Hints` is `seq_start` + `ack`, which is the specification's own
  *Per Record (TCP)* table. They travel together because every rule that uses
  one uses the other: a causal order is derived from the pair.
- {class}`~zpf.Decoded` is `decoder` + `content_type` + `spans`, which is
  exactly what the layer rule governs. `content_type` **MUST NOT** appear at
  the transport layer, `spans` are the derived-file surface, and `decoder` is
  what resolves the layer at all. The specification states two of the three as
  one object — `provenance = { decoder, spans }` — and bars the third from the
  other side of the same line.

So the split is the transport/decoded line the format is built on. A record
either speaks to a layer or it does not, and the signature says which.

## What stays flat, and why that is not inconsistency

The rule is a default, not a law: **group by the format's lines unless doing so
would make the common call worse.** Two things sit on the exception, and both
are on {meth}`~zpf.DecodeStage.record`.

**`cites=` is the hot argument of the hot path**, named on nearly every call a
decoder makes. It is decoded-layer surface by the table above and stays flat
anyway; burying it in a bundle would trade a lint count for real ergonomics.

**And that method's decoded-layer options stay flat too** — `content_type`,
`role`, `decoder`. On {meth}`~zpf.SessionWriter.record` the `Decoded` bundle
marks a line: a record either speaks to the decoded layer or does not, and the
signature says which. On a decode stage there is no line to mark, because
**every** record it writes is decoded-layer. A `decoded=` wrapper would appear
on every call and distinguish nothing, which is ceremony rather than structure.

That is why `src/zpf/` carries two `# noqa: PLR0913` rather than one. Both
comments argue rather than defer, which is the standard the restructure set for
itself.

## The builder is different again

{func}`zpf.decode_stage` carries the other suppression, for a different reason
from the one above. It is a *builder*: called once per stage with every
argument named, configuring a pipeline rather than filling in a block's fields.
`SessionWriter.record()` — the issue's real subject — passes the limit on its
own.

The one bundle available there is `produced_by` + `produced_at`, which the
format does name as a pair — a derived file MUST carry both. They are spelled
flat on {func}`zpf.create`, {func}`zpf.merge_files` and
{func}`zpf.rewrite_decoded`, so bundling them *only* in the builder would make
one of four call sites different to satisfy a count, and bundling them
everywhere is a wider break than the issue asked for. A consistent flat
spelling is worth more than the suppression costs.

## Where to go next

- [Architecture](architecture.md) — the layers this API sits on top of.
- [Contributing](contributing.md) — the spec-bump checklist, which is where a
  new option arrives from.

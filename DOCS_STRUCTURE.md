# Documentation structure — analysis and roll-out

A plan for strengthening the documentation's weakest layer: descriptive,
feature-oriented prose. Companion to the feature work tracked in
[`DECODER_API.md`](DECODER_API.md); this doc is about *structure*, not any one
feature. Work it step by step via the checklist at the end.

## The gap

The docs already cover three of the four [Diátaxis](https://diataxis.fr) modes
well, and one thinly:

| Mode | Purpose | Current state |
|------|---------|---------------|
| **Tutorial** (learning) | a guided path, once | `tutorial`, `tutorial-decoding` — good |
| **How-to** (task) | "steps for X" | `howto/*` — good |
| **Reference** (lookup) | every symbol | `api/*`, `cli`, `errors` — complete |
| **Explanation** (understanding) | "what a feature is and how to wield it" | **thin** |

`concepts.md` is the only explanation-mode page, and it describes the **model**
(spans, file kinds, ordering theory), not **feature usage**. The how-tos are
**recipe cards** — short, single-task. Nothing sits in between: a narrative,
feature-oriented layer that takes a capability (decoding, provenance, ordering,
multiple decoders) and describes it in prose with worked usage, edge cases, and
advanced patterns. New "advanced" features — the trigger for this plan was
*multiple decoders per session* — have nowhere to live.

## Recommended structure: a "Guides" layer

Add `docs/user/guides/` — one page per major feature, narrative and
usage-focused, each ending with an **Advanced** subsection where deep material
lives co-located with its feature.

```
User guide
├── Getting started        (tutorials — unchanged)
│   ├── installation
│   ├── tutorial
│   └── tutorial-decoding
│
├── Guides            ← NEW: the descriptive feature layer
│   ├── index          (map: which guide for which feature)
│   ├── reading            opening files, sessions, participants, timeline
│   ├── decoding           reassembly views, stream vs datagram, decode_stage,
│   │                      coverage & auto-fill
│   │                      └─ Advanced: multiple decoders, decoder chaining
│   ├── provenance         spans, coverage, origins, chaining raw→tls→http
│   ├── ordering           causal merge, sequenced files, SINGLE_CLOCK
│   └── faces-and-io       binary ⇄ jsonl, streaming/laziness, robustness
│
├── Concepts               (the normative model — unchanged; guides link into it)
│
├── How-to guides          (kept as short recipes; cross-linked from guides)
│   └── convert · validate · merge · decode_stage · robustness
│
└── Reference
    ├── api/index · cli · errors
```

## Three prose layers, kept distinct

The rule that stops Guides, Concepts, and How-to from overlapping:

- **Guide** — "Here is feature X, why it exists, how to use it well, and its
  advanced corners." Narrative, ~a page each. Links down to Concepts and across
  to How-to.
- **Concept** — "Here is the *model* the feature rests on." Normative
  vocabulary; the guides link into it rather than restating it.
- **How-to** — "I already know the feature — just give me the six lines for
  task Y." A recipe.

## Decisions

- **Advanced placement: (B) per-guide Advanced subsections.** *Decided.*
  Advanced material lives in an **Advanced** section at the end of the feature
  guide it extends, not in a separate top-level "Advanced" bucket — a feature's
  whole story (basic → expert) stays in one place. If a single discoverable
  entry point is wanted later, add a thin **"Advanced topics"** index page that
  *links* to those per-guide subsections; the content still lives with its
  feature. (Considered and rejected: (A) a separate Advanced bucket, which
  fragments each feature's story.)

## Where advanced topics land

- **Multiple decoders per session** → `guides/decoding.md`, Advanced section.
  A record carries its own `decoder_id`, so a session can mix decoders: declare
  each with `writer.add_decoder` (or `dec.writer.add_decoder` inside a stage),
  then pass `decoder=` per `record` / `undecoded`. Auto-fill attributes to the
  stage's default decoder. (Implemented in `DECODER_API.md` stage 8.)
- **Decoder chaining** (`raw → tls-records → http`) → same Advanced section,
  linking the provenance guide for the offset-preservation story.

## Migration is additive

Nothing has to be deleted:

- **New:** `guides/index.md` + the guide pages, plus a new toctree caption in
  [`docs/index.md`](docs/index.md) between "Getting started" and "Concepts".
- **Absorb, don't duplicate:** each existing how-to has a natural guide parent
  (`convert` → faces-and-io, `validate` → provenance, `merge` → ordering,
  `decode_stage` → decoding, `robustness` → faces-and-io). The guide owns the
  narrative; the how-to stays a terse recipe and cross-links. Trim any how-to
  that becomes a pure duplicate down to a pointer.
- **Unchanged:** tutorials, `concepts.md`, `api/*`, `cli`, `errors`, and the
  whole developer guide.

## Roll-out plan

Each step is independently shippable — a guide page at a time. Steps 2–6 are
order-independent; `decoding` first, since it is the highest-value page and
gives multiple decoders its home immediately.

- [ ] **1. Scaffold the Guides layer.** Create `docs/user/guides/index.md` (a
  short map of which guide covers which feature) and add a `Guides` toctree
  caption to [`docs/index.md`](docs/index.md), between "Getting started" and
  "Concepts". Landing page + empty toctree is enough to start; pages fill in
  below.
- [ ] **2. `guides/decoding.md` (flagship).** Reassembly views
  (`reassemble()`, `segments()`/`chunks()`/`datagrams()`), stream-oriented vs
  packet-oriented, `decode_stage`, coverage & `fill_undecoded`. **Advanced:**
  multiple decoders per session, decoder chaining. Cross-link
  `tutorial-decoding`, `howto/decode_stage`, `concepts` provenance, and the
  `api/decode` + `api/reassembly` pages.
- [ ] **3. `guides/provenance.md`.** Spans, coverage, origins, and chaining
  (`raw → tls → http`). Narrative built on the `concepts.md` provenance section
  (which stays normative). Link `howto/validate` and `api/transform`.
- [ ] **4. `guides/ordering.md`.** Causal merge, sequenced files,
  `SINGLE_CLOCK`, `timeline()` vs stored order. Link `howto/merge`, the main
  tutorial's causal-order step, and `api/order`.
- [ ] **5. `guides/reading.md`.** Opening files, sessions, participants,
  records/`timeline()`, and laziness/streaming. Link the main tutorial and
  `api/reader`.
- [ ] **6. `guides/faces-and-io.md`.** Binary ⇄ JSONL, conversion, and
  robustness (strict/lenient, truncation, diagnostics). Link `howto/convert`,
  `howto/robustness`, `api/jsonl`, `api/binary`.
- [ ] **7. Reconcile how-tos + optional advanced index.** Cross-link each guide
  and its sibling how-to both ways; trim any how-to that is now a pure duplicate
  to a pointer. If a single advanced entry point is wanted, add a thin
  `guides/advanced.md` (or a section on `guides/index.md`) linking the per-guide
  Advanced subsections — content stays with its feature (decision B).
- [ ] **8. Verify.** `.venv/bin/sphinx-build -W docs docs/_build/html` clean (no
  warnings, no broken xrefs, no orphaned pages), and every guide reachable from
  the toctree.

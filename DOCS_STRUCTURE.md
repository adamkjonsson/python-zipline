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

- [x] **1. Scaffold the Guides layer.** Created `docs/user/guides/index.md` (the
  landing page explaining the guide/concept/how-to split) with an empty toctree
  that stages 2–6 populate. Split the flat "User guide" toctree in
  [`docs/index.md`](docs/index.md) into captioned groups — **Getting started**,
  **Guides**, **Concepts**, **How-to guides** — with `cli`/`errors` folded into a
  renamed **Reference** caption alongside `api/index` (matching the plan tree).
  Added a "Want a feature in depth?" pointer to the landing links. Builds clean
  under `-W`.
- [x] **2. `guides/decoding.md` (flagship).** Reassembly views
  (`reassemble()`, `segments()`/`chunks()`/`datagrams()`), stream-oriented vs
  packet-oriented, `decode_stage`, coverage & `fill_undecoded`. **Advanced:**
  multiple decoders per session, decoder chaining. Cross-links
  `tutorial-decoding`, `howto/decode_stage`, `concepts` provenance, and the
  `api/decode` + `api/reassembly` pages; listed on `guides/index.md`. Builds
  clean under `-W`.
- [x] **3. `guides/provenance.md`.** Input sources + digests, spans, origins
  (pass-through), the coverage guarantee, and following the chain
  (`raw → tls → http`) to recover bytes. Narrative built on the `concepts.md`
  provenance section (which stays normative); links `howto/validate`,
  `decoding`, and `api/transform`; listed on `guides/index.md`. Builds clean
  under `-W`.
- [x] **4. `guides/ordering.md`.** Why timestamps aren't enough (seq/ack
  happens-before), the three consumption levels (`timeline()` / `stream()` /
  `causal_merge`), sequenced sessions + `verify()`, `SINGLE_CLOCK` for hint-less
  sessions, and merging captured directions. Links `howto/merge`, the tutorial's
  causal-order step, `concepts`, and `api/order` + `api/transform`; listed on
  `guides/index.md`. Builds clean under `-W`.
- [x] **5. `guides/reading.md`.** Opening files (`zpf.open`, faces, seekable
  sources, lenient vs strict), sessions and participants, the three record
  iterators (`records()` / `stream()` / `timeline()`), and laziness. Links the
  main tutorial, `concepts`, the ordering + decoding guides, and `api/reader`;
  listed on `guides/index.md`. Builds clean under `-W`. (Robustness and the
  face detail are pointed at `howto/robustness` and `concepts` for now; the
  stage-6 `faces-and-io` guide can add richer cross-links.)
- [x] **6. `guides/faces-and-io.md`.** One model in two encodings and when to
  pick which, face selection on read (`zpf.open` sniffing, `detect_face`,
  `face=`) and on write (`create(face=...)`), lossless conversion plus the
  canonicalization caveat, and the flat block layer
  (`BlockReader`/`BlockWriter`, `JsonlReader`/`JsonlWriter`) for non-seekable
  sources. Robustness is summarized and delegated to `howto/robustness`.
  **Advanced:** block pipelines (filter/rewrite, `check=True`) and the
  single-block helpers. Links `howto/convert`, `howto/robustness`,
  `tutorial`, `reading`, `concepts`, `api/binary` + `api/jsonl` + `api/blocks`;
  listed on `guides/index.md`. Every code sample was executed against the
  library; builds clean under `-W`.
- [ ] **7. Reconcile how-tos + optional advanced index.** Cross-link each guide
  and its sibling how-to both ways; trim any how-to that is now a pure duplicate
  to a pointer. If a single advanced entry point is wanted, add a thin
  `guides/advanced.md` (or a section on `guides/index.md`) linking the per-guide
  Advanced subsections — content stays with its feature (decision B).
- [ ] **8. Verify.** `.venv/bin/sphinx-build -W docs docs/_build/html` clean (no
  warnings, no broken xrefs, no orphaned pages), and every guide reachable from
  the toctree.
  - **Known, pre-existing:** ~148 Python xrefs across the whole doc set render
    as unlinked literals, because `api/*` documents symbols under their defining
    module (`zpf.binary.BlockReader`) while prose cites the re-export
    (`{class}`~zpf.BlockReader``). `nitpicky` is off, so `-W` does not catch it.
    `concepts.md` works around it per-link with an explicit target
    (`` {func}`zpf.binary_to_jsonl <zpf.jsonl.binary_to_jsonl>` ``); a global fix
    (canonical aliases, or `automodule:: zpf`) belongs here rather than in any
    one page.

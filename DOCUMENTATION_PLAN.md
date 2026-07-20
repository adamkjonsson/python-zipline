# Documentation plan

Plan for building out documentation for `zpf`, for two audiences:

- **Users** — people who read/write `.zpf` files with this library or its CLI.
  Pedagogical, layered, assumes no prior knowledge of the format.
- **Developers** — people who work on this codebase. Terse and succinct;
  assumes fluency with Python and the spec.

## Current state

- Docstrings are ample and Google-style throughout `src/zpf/`; Sphinx
  (autodoc + napoleon, furo theme) builds a single page, `docs/index.rst`,
  which is a quickstart plus a flat autodoc dump of all nine modules.
- `README.md` has a feature list, one example, and dev commands.
- `milestones.md` records the original design plan (four-layer API) — useful
  raw material for the developer architecture doc.
- Nothing explains the format's concepts (sessions, ticks, file kinds, causal
  ordering, the two faces) to a newcomer, and nothing orients a new developer
  in the module layout or test strategy.

## Target layout

All documentation lives under `docs/` and builds with the existing Sphinx
setup (`.venv/bin/sphinx-build docs docs/_build/html`). Pages are written in
**MyST markdown** (`.md`), not reStructuredText: add `myst-parser` to
`requirements.txt` and to `extensions` in `docs/conf.py`, and convert the
existing `docs/index.rst` as part of the restructure. Sphinx directives use
MyST fenced syntax (```` ```{automodule} ````, ```` ```{toctree} ````) and
roles use `` {func}`zpf.open` ``-style inline syntax. User docs are the
default landing content; developer docs are a clearly separated section.

```
docs/
├── index.md               # landing page: what zpf is, where to go next
├── user/
│   ├── installation.md    # pip install, Python ≥3.11, zero runtime deps,
│   │                      # the zpf-vs-zipline naming note
│   ├── tutorial.md        # the pedagogical spine (see below)
│   ├── concepts.md        # the format's mental model
│   ├── howto/
│   │   ├── convert.md     # binary ↔ JSONL, round-trip guarantees
│   │   ├── validate.md    # conformance checking, reading diagnostics
│   │   ├── merge.md       # merging two captured directions
│   │   ├── decode_stage.md# writing decode-stage files, coverage checking
│   │   └── robustness.md  # truncated files, unknown blocks, lenient reads
│   ├── cli.md             # zpf info/cat/convert/validate/merge reference
│   └── errors.md          # exception hierarchy, structural vs semantic,
│                          # what Diagnostic gives you
├── api/
│   ├── index.md           # one page per module instead of today's dump:
│   ├── reader.md          #   ergonomic layer first (reader, writer),
│   ├── writer.md          #   then blocks, binary, jsonl, order,
│   ├── blocks.md          #   transform, conformance, errors
│   └── ...
└── dev/
    ├── architecture.md    # the four layers, module map, data flow
    ├── testing.md         # test suite map: golden bytes, property tests,
    │                      # conformance vectors; how to add a test
    ├── conformance.md     # how spec rules map to code (enforcement in
    │                      # writer vs checking in ConformanceChecker)
    └── contributing.md    # workflow: venv, pytest, ruff, docs, wheel,
                           # release steps; style rules (type hints,
                           # docstring format)
```

Plus one file outside `docs/`:

- `CONTRIBUTING.md` (root) — a short pointer: setup commands and a link to
  `docs/dev/`. Keeps the GitHub-visible entry point without duplicating
  content.

## User docs: pedagogical shape

Ordered so each page needs only the ones before it.

1. **Landing page** — what problem `.zpf` solves (reassembled payload +
   metadata, not packets), one small code sample, links onward. Keep it
   shorter than the current index.
2. **Installation** — trivial, but its own page so the tutorial starts clean.
3. **Tutorial** (the core, ~4 progressive stages, one runnable script each):
   a. *Write your first file* — `zpf.create`, one session, two participants,
      a request/response pair. Introduces tick_hz, sources, seq/ack minimally.
   b. *Read it back* — `zpf.open`, sessions, records, lazy access.
   c. *Causal order* — why wall clocks lie across capture points;
      `session.timeline()`; the merge transform and SEQUENCED.
   d. *The two faces* — dump to JSONL with the CLI, inspect by eye, convert
      back; establishes that the faces are lossless projections.
4. **Concepts** — the reference mental model the tutorial only sketched:
   file kinds (raw / decode-stage / pass-through), timebase and ticks,
   sessions/participants/records, spans and origins, the SEQUENCED flag,
   preservation of unknown blocks. Diagrams welcome (a session's block
   sequence; the two-direction merge).
5. **How-to guides** — task-shaped, minimal prose, one task per page (list
   above). These may assume the tutorial.
6. **CLI reference** — every subcommand with flags, exit codes, and a worked
   example; note which library call each command wraps.
7. **API reference** — restructured autodoc, ergonomic layer first. Each
   module page opens with 2–3 sentences on when you'd use this layer.
8. **Errors page** — the `ZpfError` hierarchy, structural-vs-semantic tiers,
   truncation as an expected condition, working with `Diagnostic`.

Style: second person, present tense, every example complete and runnable
(doctest or literalinclude from tested example scripts where practical so
examples can't rot). Spell out format vocabulary on first use and link to the
spec section it comes from.

## Developer docs: terse shape

Bullet-heavy reference pages, not prose. Content sources already exist:

- **architecture.md** — distill `milestones.md`'s four-layer description to
  its final form (layers: blocks → block I/O (binary + jsonl) → ergonomic
  reader/writer → transforms + CLI). One table mapping module → layer →
  responsibility. Note the invariants that shape the design: byte-faithful
  preservation of unknown blocks, writers enforce conformance at write time,
  readers isolate semantic errors but reject structural ones.
- **testing.md** — table of test files → what they cover; call out the
  golden-bytes test (spec's worked example), the hypothesis round-trip
  property, and the skewed-clock merge integration test as the load-bearing
  ones. How to run subsets.
- **conformance.md** — where each class of spec rule is enforced
  (writer-time vs `ConformanceChecker` vs `check_coverage`), and the policy
  for anything that goes beyond the standard (must be flagged — see
  CLAUDE.md).
- **contributing.md** — commands (venv, pytest, ruff, sphinx, build),
  code style rules, docstring conventions, release checklist (version bump in
  `__init__.py`, changelog, wheel).

## Work plan

Roughly in dependency order; each step leaves the docs buildable and useful.

1. **Restructure scaffolding** — install and enable `myst-parser`; split
   `docs/index.rst` into the MyST tree above with stubs (deleting the `.rst`
   file); move the existing autodoc directives into `docs/api/` pages; add
   toctrees. No new prose yet. (Small) — **Done** (2026-07-19): builds
   clean under `sphinx-build -W`.
2. **Concepts + landing page** — the mental-model page, since tutorial and
   how-tos link into it. (Medium) — **Done** (2026-07-19); also fixed the
   stage-1 API pages to wrap autodoc in `{eval-rst}` and enabled
   `myst_heading_anchors`.
3. **Tutorial** — the four stages, with runnable example scripts checked
   into `docs/user/examples/` and exercised by a test so they stay green.
   (Large; the highest-value item) — **Done** (2026-07-20): one running
   example (a GET/200-OK exchange, matching the golden-bytes and
   merge-transform test fixtures) carried through all four stages; scripts
   run for real in `tests/test_tutorial_examples.py`; builds clean under
   `sphinx-build -W`.
   - *Follow-on beyond the original four-stage scope* — **Done**
     (2026-07-20): a companion **decoder tutorial**
     (`docs/user/tutorial-decoding.md`) walks raw → decode-stage on a small
     REST call: the logical-offset model, writing spans and `content_type`,
     the coverage guarantee and `Undecoded` markers. Three more example
     scripts (`05`–`07`), also run by `test_tutorial_examples.py`. The terse
     `howto/decode_stage.md` recipe (stage 4 below) links to it.
4. **How-to guides + CLI reference + errors page.** (Medium)
5. **Developer docs** — architecture, testing, conformance, contributing;
   add root `CONTRIBUTING.md` pointer. Retire `milestones.md` (its content is
   absorbed; keep it in git history). (Medium)
6. **Polish pass** — README trimmed to point at the built docs; check every
   page for spec-link accuracy; `sphinx-build -W` clean (warnings as
   errors) added to the dev checklist. (Small)

## Conventions for all docs

- Build must stay warning-free; docstring changes follow the existing Google
  style + ruff rules from `ruff.toml`.
- Never document behavior beyond the v1.0 spec without an explicit callout
  box saying so.
- All pages are MyST markdown; no new `.rst` files. Cross-reference with
  MyST roles (`` {func}`zpf.open` ``, `` {class}`zpf.FileReader` ``) rather
  than bare code literals, so references break loudly when the API changes.
  Module docstrings stay Google-style rST as napoleon expects — MyST applies
  to the doc pages, not to docstrings.

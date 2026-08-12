# Plans and spec reviews

Working documents: the migration plans that carried this library between spec
versions, the reviews we wrote of each upstream draft, and the API design
proposals that preceded a feature. They are kept because they record *why* a
thing was built the way it was — an argument that neither the code nor the
commit log states in full.

**They are historical, not normative.** A plan describes what was intended at
the time it was written, and several describe spec versions this library no
longer implements. Where one disagrees with the code, the code is right; where
one disagrees with `CLAUDE.md` or `docs/`, those are right. Nothing here is
maintained after the work it describes has landed.

The live records stay in the project root: [`CHANGELOG.md`](../CHANGELOG.md)
for what shipped, and [`VECTOR-DEFECTS.md`](../VECTOR-DEFECTS.md) for the
conformance-vector defect register, which is still open and is referenced by
the test suite.

## Spec ports

| Document | What it is |
| --- | --- |
| [`SPEC-0.12-MIGRATION-PLAN.md`](SPEC-0.12-MIGRATION-PLAN.md) | The 0.9 → 0.12 port, phases 0–7. |
| [`SPEC-0.13-REVIEW.md`](SPEC-0.13-REVIEW.md) | Our review of the 0.13 draft; seven findings, all fixed by 0.14. |
| [`SPEC-0.14-MIGRATION-PLAN.md`](SPEC-0.14-MIGRATION-PLAN.md) | The 0.12 → 0.14 port, phases 0–6. |
| [`SPEC-0.14-FINDINGS.md`](SPEC-0.14-FINDINGS.md) | What that port found in 0.14 itself, kept apart from the plan. |
| [`SPEC-0.15-REVIEW.md`](SPEC-0.15-REVIEW.md) | Why 0.15 was unimplementable as written; its findings were adopted into 0.16. |
| [`SPEC-0.16-MIGRATION-PLAN.md`](SPEC-0.16-MIGRATION-PLAN.md) | The 0.14 → 0.16 port, phases 0–8. The version the library implements today. |

## API and documentation design

| Document | What it is |
| --- | --- |
| [`DECODER_API.md`](DECODER_API.md) | The decoder-facing read API — reassembly views, span helpers, the decode-stage orchestrator. |
| [`CONTENT_TYPE_API.md`](CONTENT_TYPE_API.md) | Reading a record's payload per its `content_type` label. |
| [`DOCS_STRUCTURE.md`](DOCS_STRUCTURE.md) | The guides layer, and how the documentation tree is arranged. |

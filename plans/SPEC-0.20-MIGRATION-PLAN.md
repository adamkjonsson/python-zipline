# Migration plan: `python-zipline` 0.19 → 0.20

Against the [0.20 specification](https://github.com/adamkjonsson/zipline/blob/v0.20/docs/zipline-payload-format.md),
its [CHANGELOG](https://github.com/adamkjonsson/zipline/blob/v0.20/CHANGELOG.md)
and the 55 vectors at tag `v0.20` (commit `55d4993`, cut 2026-09-14). We ship
`0.3.0` on `0.19`; this is a one-release jump.

> **Status, 2026-09-14: every phase is done**, on branch `spec-0.20`. The
> suite is 935 passed, zero xfail, zero xpass; `ruff check` and a `-W` Sphinx
> build are clean. Nothing in `src/` changed except the version constant and
> two docstrings, which is what the scratch run predicted.

---

## What we are actually migrating to

**`0.20` is a repair-and-vector release, and two of its four headline items were
filed from this repository.** No option and no block is added, no body layout
moves, and no `.jsonl` changed. What moved:

1. **The version stamp.** `0`/`20`. Every `.zpf` in the suite is regenerated
   for that alone, which is why 178 files change and only a handful mean it.
2. **Defects 5 and 6 are fixed** ([#141](https://github.com/adamkjonsson/zipline/issues/141),
   ours). `handshake-at-origin`, `unplaceable-below-origin` and
   `mixed-derivation` now have bytes that agree with their projections. Upstream
   also closed the door the defects came through: `build.py` now projects every
   vector at registration and refuses one whose two faces disagree.
3. **A pass-through preserving a transport stream MUST mark each hole of its
   input** ([#133](https://github.com/adamkjonsson/zipline/issues/133)) — a new
   MUST stating a consequence `0.19` already implied, with a new fixture
   (`merge/`, three files, accept) and a negative twin
   (`isolate-merge-unmarked-hole`, one file, isolate). The upstream changelog
   names this library as the implementation that "has already built to the
   obligation as written".
4. **`extents` in `manifest.json`** ([#140](https://github.com/adamkjonsson/zipline/issues/140),
   ours) — every single-file accept vector now declares, per participant
   stream, the extent a reader must compute. 31 vectors, 37 streams. This is
   the data `_ACCEPT_EXTENTS` in `tests/test_vectors.py` was transcribing by
   hand while waiting for it.
5. **`retransmit` names the sender's act** ([#142](https://github.com/adamkjonsson/zipline/issues/142)).
   A duplicated capture — every packet seen twice — is not a retransmission
   and does not set the flag; what the reassembler discarded is an Undecoded
   block, as it always was. New SHOULD for a producer that cannot tell the
   two apart. No vector, because a reader treats the bit identically.
6. **Decoder `output_layer` is named load-bearing beside Source `kind`** in
   the unrecognised-enum paragraph. A statement of what
   `isolate-unknown-output-layer` already pinned.
7. **Thirteen leftovers of `0.19`'s deletions** removed from the text —
   `origin`, `flags`/`single_clock`, the "see below" with nothing below.

**Vectors: 53 → 55**, 59 → 63 files, no removals.

---

## What the suite said before any code was written

Phase 0 of this plan was run in a scratch copy first: vectors at `v0.20`,
`SPEC_VERSION` moved, nothing else. Result, `tests/test_vectors.py`:

- **274 passed, 9 XPASS, 1 xfail, 1 failed.**
- The failure is `test_every_case_has_a_file`'s exact count (59 → 63).
- The nine XPASSes are the three `DEFECTIVE` names, each in both `test_accept`
  and the JSONL → binary direction, plus `merge/{a,b,merged}` in `test_accept`.
- The one xfail is `isolate-merge-unmarked-hole`, and it is **not** a missing
  check: the reader already reports `input stream (source 1, session 7, pid 0):
  [16, 35) is neither decoded nor marked Undecoded`, and the checked writer
  refuses the file. It xfails because `_ISOLATE_REASONS` has no entry for the
  name, and the `KeyError` is swallowed by `strict=False`.
- All 37 declared `extents` match `zpf.stream_extent` — checked by a script,
  before the harness was taught to read them.
- Projecting all 43 files that carry both faces against their `.jsonl`: **zero
  disagreements.** No new defect at `v0.20`.

The rest of the suite, with the two example files' `zipline-payload/0.19`
strings bumped: **893 passed, 2 failed** — the `(0, 19)` golden assertion and
the `Specification` URL test. Both are version literals the checklist in
`docs/dev/contributing.md` names.

So the port is: literals, the re-vendor, harness bookkeeping, and prose. **No
rule is implemented in this port.** Every behavioural change `0.20` makes was
already in the library at `0.3.0`.

---

## Decisions

### D1 — `_ACCEPT_EXTENTS` reads the manifest

The table's own comment promised it: "if they ever are [declared as data],
this table reads them instead of transcribing them from each vector's `expect`
prose." They are. The table goes; the test parametrizes over every manifest
entry carrying `extents` and asserts each stream. 2 assertions become 37, and
the two hand-transcribed numbers are among them unchanged (16 and 6).

`_EXTENTS_PENDING` stays as the hold set, empty, because the ratchet shape is
the point: an extent we get wrong at a future re-vendor is held by name, not
by deleting the assertion.

### D2 — `DEFECTIVE` empties, and stays declared

All three entries leave. The dict stays, empty, with its docstring rewritten
to record that it emptied at `v0.20` and why the discipline it encodes (never
bend the implementation to a fixture) outlives the entries.

### D3 — `retransmit` is a docstring change here

The library holds no reassembler that emits records from packets;
`StreamView` consumes records a producer already reassembled. Nothing in
`src/` sets `RecordFlags.RETRANSMIT`. The change lands as the enum member's
docstring, one sentence in `docs/user/concepts.md`, and nothing else — a
producer written against this library reads the flag's meaning from the enum.

### D4 — library version becomes `0.4.0.dev0`

A spec port is a library minor bump (`docs/dev/contributing.md` § Releasing),
and `main` should carry a `.dev0` between releases. `0.3.0` shipped without
that suffix; this port sets it.

---

## Phases

Each phase ends green under `.venv/bin/pytest` and `ruff check`.

### Phase 0 — re-vendor the suite

Copy `vectors/` at `v0.20` verbatim (excluding `build.py`/`check.py`), verify
with `diff -r`, update `tests/vectors/VENDORED.md`. Run the projection sweep.
The suite is *red* at the end of this phase — every `.zpf` is refused at the
gate — which is the correct state.

### Phase 1 — move the gate, and every literal

`SPEC_VERSION = (0, 20)`. `Specification` URL in `pyproject.toml`. The golden
assertion in `tests/test_golden.py`. `zipline-payload/0.19` in
`tests/test_jsonl.py` and `docs/user/cli.md`. Version to `0.4.0.dev0`.

### Phase 2 — the harness

`KNOWN_PASSING` +7 (three from `DEFECTIVE`, `merge/a`, `merge/b`,
`merge/merged`, `isolate-merge-unmarked-hole`) → 62 names. `DEFECTIVE` empty
(D2). `_ISOLATE_REASONS["isolate-merge-unmarked-hole"] = "neither decoded nor
marked"`. Case count 63. `_ACCEPT_EXTENTS` from the manifest (D1). Module and
data docstrings retold for `0.20`.

### Phase 3 — prose

`CLAUDE.md` (which still said `0.16`), `README.md`, `docs/index.md`,
`docs/user/`, `docs/dev/`, the stale `Note` on `check_extents` that still says
a pass-through "has no `spans`", `RecordFlags.RETRANSMIT` (D3),
`VECTOR-DEFECTS.md` closing defects 5 and 6 at `v0.20`.

### Phase 4 — the changelog

An `[Unreleased]` entry naming `0.20`, stating that `0.3.0` files are refused
at the gate, and listing what a consumer sees: `check_extents` now finds an
unmarked hole in a merge (it already did — but the fixture that proves it is
new), the three vectors returning, nothing removed from the API.

---

## What this does not change

- No public API. No block, option, enum or field.
- `merge_files`, `check_coverage`, `check_extents`, the `CoverageLedger` —
  all already state the pass-through obligation. `0.20` catches up to them.
- `UNIMPLEMENTED` stays empty.

## Sizing

Half a day. Phases 0–2 are mechanical and were rehearsed in the scratch run;
Phase 3 is the only one with judgement in it, and it is a sweep of roughly
twenty files most of which need one number changed.

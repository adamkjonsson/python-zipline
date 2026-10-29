# python-zipline — Claude guidance

## The product

This code is a Python implementation of v0.24 of the Zipline Payload Format,
which is defined in
`https://github.com/adamkjonsson/zipline/blob/v0.24/docs/zipline-payload-format.md`.
A checkout of the spec repository lives at `~/projs/zipline`; a new spec release
is a new tag there, and `v1.0` is the *old* one (see the first trap below).
It is a module that readers and writers of zpf-files use to access and create
files.

"The standard" in this file always means **0.24**, and `zpf.SPEC_VERSION` is the
single source of truth for it in code. The format is in `0.x`, where **every
minor is a separate format**: a reader must reject a `version_minor` it does not
implement, and no upgrade path between `0.x` versions is guaranteed. So this is
a single-version library — there is deliberately no 0.9 or 0.10 compatibility
path, and files written by earlier versions of this library are unreadable by it.

Five traps worth naming, because each looks like a bug and is not:

- A **0.9** file stamps `version_major = 1`, `version_minor = 0` — that version
  was published as "1.0" and renumbered without rewriting its bytes, so the
  `v1.0` tag sorts *above* every `0.x` tag and is the oldest of them. 0.24
  stamps `0`/`24`. A 0.9 file is correctly rejected at the version gate.
- `decoder_id` decides **neither** axis. It does not say a record is decoded —
  reassembly is a decoder too, so the layer comes from that decoder's declared
  `output_layer` — and it does not say which stage ran, since a pass-through
  carries inherited ones forward. Both axes are per **stream**, never per file.
  There is no file kind; `FileReader.stream_kind(session_id, pid)` is the
  accessor.
- **Every `zpf`-sourced record carries `spans`**, and created versus preserved
  is read from whether they are *identity* spans — the same range in as out.
  Not from an option: `origin` was removed in `0.19`, and with it the four
  rules that policed which of the two a stream was. A pass-through therefore
  cites its inputs, which makes it answerable for their coverage the way a
  decode stage is.
- **Offsets unwrap along stored order** (since `0.21`), **and so do acks**
  (since `0.22`). A transport record's offset is its *predecessor's* plus the
  signed serial delta of their `seq_start`s — never `seq_start − origin` under
  serial arithmetic, which read every record more than 2³¹ bytes into a stream
  as below the origin. The origin is the first record's predecessor, and an
  unplaceable record anchors nothing. An `ack` is a **signed position in the
  peer's offset space**, walked the same way along the acker's records, the
  first measured from the peer's origin; the merge and the sequenced check
  compare it with record *ends* as plain integers, never serially. Since
  `0.23` a record's end is one past the last **sequence number** it occupies:
  data ends at offset + length, a zero-length SYN at its offset, a zero-length
  FIN at offset + 1, and a pure ACK or bare RST has **no end**, so no edge runs
  from it (`_sequence_end` in `order.py`). **An extent is reach, not end**
  (since `0.24`): a record reaches offset + length, so a zero-length one
  reaches its own offset, and a zero-length FIN ends one further than it
  reaches. A final pure ACK stored one past its FIN puts the FIN's position
  inside the extent, and that position is owed no coverage
  (`fin_positions` in `reassembly.py`). One file cannot tell it from a
  dropped byte, so the single-file extent check tolerates exactly one. Both walks
  are `_Walk` in `order.py` (`_Placer` for offsets, `_AckWalk` for acks), and
  the checker and the writer's guard use the same anchor. A stream past
  2 GiB, or one whose sequence numbers pass through 2³², is conformant,
  places, and orders.
- **`adjacency` is a Participant body field, and the third load-bearing enum.**
  `contiguous` (`0`) is what every file ever written holds there; `units` (`1`)
  declares a *unit sequence* — offsets unchanged, no two adjacent records may
  be assumed to join, no Discontinuity owed at any seam. An unrecognised value
  is isolated, never defaulted to `contiguous`. On a transport-layer participant
  the field says nothing and a reader ignores it (advisory). `StreamView.units()`
  surfaces a unit sequence as a `Break(declared=False)` before every record
  after the first — **that is this library's reading, not the format's**, and
  the docs say so wherever it appears.

The conformance vectors in `tests/vectors/` are vendored verbatim from the spec
repository and are the acceptance criteria — do not edit them to make a test
pass. `VECTOR-DEFECTS.md` records eight defects found against them, seven
closed upstream. The eighth (zipline#175, first-packet stamps on three
sessionizer vectors) is in conformant files, so it holds nothing out, and
`DEFECTIVE` in `tests/test_vectors.py` is empty at `v0.24`. When a
fixture is wrong, it goes in `DEFECTIVE` by name and is reported upstream —
never bend the implementation to match a fixture in `DEFECTIVE`.

The 0.23 → 0.24 port is **in progress**, as the `0.8.0` release
(`plans/V0.8.0-RELEASE-PLAN.md`, issues #104–#108); the earlier ports are in
`plans/` too, and `docs/dev/contributing.md` § *Porting to a new spec version*
is the checklist. The vectors are vendored at `v0.24` — 77 of them, 90 files.
Every name is in `KNOWN_PASSING` except `decoded-past-fin/decoded` (#104) and
`isolate-copy-differs` (#106), which the port's later phases earn. Ten of them
declare a `merged` order, which the harness compares `timeline()` against; since
`0.24` a multi-file entry's items name their member file. The ratchet in
`tests/test_vectors.py` stays as the regression guard: a name is never removed
from that set while its vector exists.

Two vectors are judged as a **pair**: `splice` and `isolate-transport-splice`
each ship two files that are conformant alone, so the violation is only visible
to `zpf.check_splice`. The harness routes any multi-file vector in a negative
tier that way. The multi-file *accept* vectors get the mirror check: each hop
upstream vouches for must pass `check_coverage` and `check_splice` as a pair.

The API should be easy to use and feel logical. It must always follow the standard. The support for the standard should be complete. Always warn if a feature requires the code to go beyond the standard.

## Code style

- **Type hints everywhere.** All function parameters, return types, and class attributes must be annotated. Use `from __future__ import annotations` at the top of every module.
- **Zero ruff warnings.** After any change, the file you touched must produce no warnings from `ruff check`. The project config is in `ruff.toml`. Never make a warning
go away by changing values in the config file. Using a # noqa: comment to suppress
warnings can only be used as a last resort, and **always ask before making such a change**.

## Git

- **Never commit or push without explicit instruction.** Do not run `git commit`, `git push`, or any destructive git command (`reset --hard`, `checkout .`, etc.) unless I have asked for it in the current message.
- **Never push directly to `main`.** Every change reaches it through a pull
  request, including a one-line docs fix, and including when I have asked for
  a commit — "commit this" means *on a branch*, never on `main`. `main` carries
  a GitHub rule requiring a pull request; a bypass permission makes that rule
  advisory rather than enforced, so the discipline has to come from here. It
  went wrong once, closing the `0.7.0` plan: a docs-only commit was pushed
  straight to `main` and the remote reported *Bypassed rule violations*. It
  could not be undone — the `non_fast_forward` rule **is** enforced, so the
  commit is still in the history. Asking first costs a minute; the bypass is
  not reversible.
- If `main` is checked out and a commit is wanted, create the branch first.
  That is the one branching decision to take without asking, because the
  alternative is the mistake above.

## Project layout

- `src/zpf/` — contains the code (import name `zpf`; the `zipline` name on PyPI belongs to an unrelated project)
- `docs/` — documentation of the code
- `tests/` — test suite, native pytest style; run with `.venv/bin/pytest`

## Virtual environment

All development tasks (tests, docs, wheel builds) use a single venv created from `requirements.txt`:

```bash
python -m venv .venv
.venv/bin/pip install -e . -r requirements.txt
```

- **Run tests:** `.venv/bin/pytest`
- **Build docs:** `.venv/bin/sphinx-build docs docs/_build/html`
- **Build wheel:** `.venv/bin/python -m build`

## Conventions

- Use `.venv/bin/pytest` to run tests and `ruff` (on PATH) to lint.
- Docstrings follow Google style with ruff-enforced formatting (see `ruff.toml`). Sections (Args, Returns, Raises, Attributes, Example) need a blank line before the closing `"""`.

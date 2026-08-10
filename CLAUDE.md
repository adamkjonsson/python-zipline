# python-zipline — Claude guidance

## The product

This code is a Python implementation of v0.16 of the Zipline Payload Format,
which is defined in
`https://github.com/adamkjonsson/zipline/blob/v0.16/docs/zipline-payload-format.md`.
It is a module that readers and writers of zpf-files use to access and create
files.

"The standard" in this file always means **0.16**, and `zpf.SPEC_VERSION` is the
single source of truth for it in code. The format is in `0.x`, where **every
minor is a separate format**: a reader must reject a `version_minor` it does not
implement, and no upgrade path between `0.x` versions is guaranteed. So this is
a single-version library — there is deliberately no 0.9 or 0.10 compatibility
path, and files written by earlier versions of this library are unreadable by it.

Two traps worth naming, because both look like bugs and are not:

- A **0.9** file stamps `version_major = 1`, `version_minor = 0` — that version
  was published as "1.0" and renumbered without rewriting its bytes. 0.16 stamps
  `0`/`16`. A 0.9 file is correctly rejected at the version gate.
- `decoder_id` decides **neither** axis. It does not say a record is decoded —
  reassembly is a decoder too, so the layer comes from that decoder's declared
  `output_layer` — and it does not say which stage ran, since a pass-through
  carries inherited ones forward. Created versus preserved is `spans` versus
  `origin`, and both axes are per **stream**, never per file. There is no file
  kind; `FileReader.stream_kind(session_id, pid)` is the accessor.

The conformance vectors in `tests/vectors/` are vendored verbatim from the spec
repository and are the acceptance criteria — do not edit them to make a test
pass. `VECTOR-DEFECTS.md` records four defects found against them: three closed,
and defect 4 open at `v0.16`, which holds `tunnel/inner` and `tunnel/outer` in
`DEFECTIVE`. Never bend the implementation to match a fixture in `DEFECTIVE`.

The 0.14 → 0.16 port is **complete** (`SPEC-0.16-MIGRATION-PLAN.md`, phases 0–8).
The vectors are vendored at `v0.16` — 53 of them, 59 files — and every name is in
`KNOWN_PASSING`. The ratchet in `tests/test_vectors.py` stays as the regression
guard: a name is never removed from that set.

One vector is judged as a **pair**: `splice` ships two files that are each
conformant alone, so its violation is only visible to `zpf.check_splice`. The
harness routes any multi-file vector in a negative tier that way.

The API should be easy to use and feel logical. It must always follow the standard. The support for the standard should be complete. Always warn if a feature requires the code to go beyond the standard.

## Code style

- **Type hints everywhere.** All function parameters, return types, and class attributes must be annotated. Use `from __future__ import annotations` at the top of every module.
- **Zero ruff warnings.** After any change, the file you touched must produce no warnings from `ruff check`. The project config is in `ruff.toml`. Never make a warning
go away by changing values in the config file. Using a # noqa: comment to suppress
warnings can only be used as a last resort, and **always ask before making such a change**.

## Git

- **Never commit or push without explicit instruction.** Do not run `git commit`, `git push`, or any destructive git command (`reset --hard`, `checkout .`, etc.) unless I have asked for it in the current message.

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

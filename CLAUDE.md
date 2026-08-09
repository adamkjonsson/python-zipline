# python-zipline — Claude guidance

## The product

This code is a Python implementation of v0.14 of the Zipline Payload Format,
which is defined in
`https://github.com/adamkjonsson/zipline/blob/v0.14/docs/zipline-payload-format.md`.
It is a module that readers and writers of zpf-files use to access and create
files.

"The standard" in this file always means **0.14**, and `zpf.SPEC_VERSION` is the
single source of truth for it in code. The format is in `0.x`, where **every
minor is a separate format**: a reader must reject a `version_minor` it does not
implement, and no upgrade path between `0.x` versions is guaranteed. So this is
a single-version library — there is deliberately no 0.9 or 0.10 compatibility
path, and files written by earlier versions of this library are unreadable by it.

Two traps worth naming, because both look like bugs and are not:

- A **0.9** file stamps `version_major = 1`, `version_minor = 0` — that version
  was published as "1.0" and renumbered without rewriting its bytes. 0.14 stamps
  `0`/`14`. A 0.9 file is correctly rejected at the version gate.
- `decoder_id` does **not** decide a file's kind. The discriminator between the
  two derived kinds is `spans` versus `origin`; a pass-through preserving a
  decoded layer carries inherited `decoder_id` values forward.

The conformance vectors in `tests/vectors/` are vendored verbatim from the spec
repository and are the acceptance criteria — do not edit them to make a test
pass. `VECTOR-DEFECTS.md` records four defects found against them: three closed,
and defect 4 open at `v0.16`, which holds `tunnel/inner` and `tunnel/outer` in
`DEFECTIVE`. Never bend the implementation to match a fixture in `DEFECTIVE`.

**A port to 0.16 is in progress** (`SPEC-0.16-MIGRATION-PLAN.md`), phases 0 and
1 landed. The vectors are vendored at `v0.16` — 53 of them, 59 files — and the
version gate now reads 0.16, so 0.14 files are refused. The rest of the suite is
behind the ratchet in `tests/test_vectors.py`: a vector is a hard requirement
only once its name is in `KNOWN_PASSING`, which only ever grows.

Until the later phases land, the **prose** — this file's version statements
above, `README.md`, `docs/` — still describes 0.14, and the code above it does
not. Phase 8 is the pass that fixes that; do not treat the mismatch as guidance
in either direction, read `SPEC_VERSION` and the plan.

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

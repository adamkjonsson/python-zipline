# Contributing

Working on `zpf` itself. All development tasks use a single virtualenv built
from `requirements.txt`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements.txt
```

Python ≥ 3.11. The package has zero runtime dependencies; everything in the
venv is a dev tool (pytest, ruff, sphinx, build, hypothesis).

## The quality gate

Run all three before pushing; they are the same checks the project treats as
release-blocking.

```bash
.venv/bin/pytest                              # tests — must be green
ruff check .                                  # lint — must be zero warnings
.venv/bin/sphinx-build -W docs docs/_build/html   # docs — warnings are errors
```

The docs also build with the `docs/Makefile`: `cd docs && make html` (add
`SPHINXOPTS=-W` to match the gate).

## Code style

The rules in `CLAUDE.md` and `ruff.toml` are enforced, not aspirational:

- **Type hints everywhere** — every parameter, return, and class attribute
  annotated. `from __future__ import annotations` at the top of every module.
- **Zero ruff warnings.** After a change, the files you touched must produce
  none from `ruff check`. Don't relax `ruff.toml` to silence a warning, and
  treat a `# noqa` as a last resort that needs a maintainer's sign-off first.
- **Google-style docstrings**, ruff-formatted. Sections (`Args`, `Returns`,
  `Raises`, `Attributes`, `Example`) need a blank line before the closing
  `"""`. Module docstrings stay Google-style reStructuredText (napoleon reads
  them); MyST is for the doc *pages*, not docstrings.
- The `ruff.toml` lint set is broad (pydocstyle, annotations, bugbear,
  simplify, pylint, …). Tests are exempt from docstring and return-annotation
  rules via per-file ignores; nothing else is exempt.

## Docs

- User and developer pages are **MyST markdown** under `docs/`; no new `.rst`.
- Cross-reference with MyST roles (`` {func}`zpf.open` ``,
  `` {class}`zpf.FileReader` ``) rather than bare code literals, so a rename
  breaks the build.
- Never document behavior beyond the v1.0 spec without an explicit callout
  saying so (see [Conformance strategy](conformance.md#going-beyond-the-standard)).
- Tutorial and how-to code examples are checked into `docs/user/examples/` and
  run by `test_tutorial_examples.py` — extend that test when you add one, so
  examples can't rot.

## Releasing

The version is **dynamic**: hatch reads `__version__` from
`src/zpf/__init__.py` (`[tool.hatch.version]` in `pyproject.toml`). To cut a
release:

1. Bump `__version__` in `src/zpf/__init__.py` (PEP 440;
   `test_package.py` checks the shape).
2. Run the full quality gate above — all green.
3. Build the artifacts:
   ```bash
   .venv/bin/python -m build          # wheel + sdist into dist/
   ```
4. Tag the commit and write the release notes on the tag / GitHub release
   (the repo keeps no separate changelog file).

## Git conventions

- Never commit or push without being asked; never run destructive git
  commands (`reset --hard`, `checkout .`, …) unasked.
- Branch off `main` for changes; keep the working tree lint- and test-clean
  per commit.

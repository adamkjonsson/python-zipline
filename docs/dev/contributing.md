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
- **Keep a colon out of the first line of a `Returns:` block** (and out of a
  one-line property docstring). Napoleon reads `text: more text` there as
  `type: description`, so the words before the colon vanish from the rendered
  page and turn into a bogus type link. Use an em dash instead.
- The `ruff.toml` lint set is broad (pydocstyle, annotations, bugbear,
  simplify, pylint, …). Tests are exempt from docstring and return-annotation
  rules via per-file ignores; nothing else is exempt.

## Docs

- User and developer pages are **MyST markdown** under `docs/`; no new `.rst`.
- Cross-reference with MyST roles (`` {func}`zpf.open` ``,
  `` {class}`zpf.FileReader` ``) rather than bare code literals, so a rename
  breaks the build. Cite the **top-level re-export** (`zpf.FileReader`), the
  way a consumer imports it: `api/*` documents each symbol under the module
  that defines it, and a `missing-reference` hook in `docs/conf.py` bridges
  the two using `zpf.__all__`.
- The build runs with `nitpicky = True`, so an unresolvable reference — a
  typo, a renamed symbol, a dead heading anchor — is a warning, and `-W`
  makes it an error. `nitpick_ignore` in `docs/conf.py` lists the handful of
  genuine exceptions (standard-library names, which would otherwise need a
  network-dependent intersphinx fetch); don't add to it to silence a real
  break.
- Never document behavior beyond the v0.16 spec without an explicit callout
  saying so (see [Conformance strategy](conformance.md#going-beyond-the-standard)).
- Tutorial and how-to code examples are checked into `docs/user/examples/` and
  run by `test_tutorial_examples.py` — extend that test when you add one, so
  examples can't rot.

## Releasing

The version is declared **once**, as `version` under `[project]` in
`pyproject.toml`. `zpf.__version__` reads it back from the installed
distribution metadata, so nothing in `src/` restates it and the two can
never disagree.

Numbering follows the same rule as the format, on its own counter: while we
are in `0.x`, every minor is a break and no upgrade path is promised. A port
to a new spec minor is therefore always a library minor bump — the format
guarantees no upgrade path across one, so neither can we. The library number
and `zpf.SPEC_VERSION` are separate and are not expected to match.

To cut a release:

1. Bump `version` in `pyproject.toml` (PEP 440; `test_package.py` checks the
   shape).
2. Move the `[Unreleased]` entries in `CHANGELOG.md` under the new version
   with today's date, note the spec version it implements, and add a fresh
   empty `[Unreleased]` section plus the compare links at the foot.
3. Run the full quality gate above — all green.
4. Build the artifacts:
   ```bash
   .venv/bin/python -m build          # wheel + sdist into dist/
   ```
5. Tag the commit `vX.Y.Z` and use that version's changelog section as the
   GitHub release notes.

Changelog entries follow [Keep a Changelog 1.1.0][kac]: written for the
people consuming the library, grouped under Added / Changed / Deprecated /
Removed / Fixed / Security, and never a dump of the commit log. Add the entry
under `[Unreleased]` in the same pull request that makes the change, so the
release step is only a rename.

[kac]: https://keepachangelog.com/en/1.1.0/

## Git conventions

- Never commit or push without being asked; never run destructive git
  commands (`reset --hard`, `checkout .`, …) unasked.
- Branch off `main` for changes; keep the working tree lint- and test-clean
  per commit.

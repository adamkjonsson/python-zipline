# python-zipline — Claude guidance

## The product

This code is a Python implementation of v1.0 of the Zipline Payload Format, which
is defined in `https://github.com/adamkjonsson/zipline/releases/tag/v1.0`. It is a
module that readers and writers of zpf-files use to access and create files.

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

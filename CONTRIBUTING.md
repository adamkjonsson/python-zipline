# Contributing to python-zipline

Quick start:

```bash
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements.txt

.venv/bin/pytest                                   # tests
ruff check .                                       # lint (zero warnings)
.venv/bin/sphinx-build -W docs docs/_build/html    # docs (warnings are errors)
```

The full contributor guide — code style, docstring conventions, the
conformance strategy, the test suite map, and the release checklist — lives in
the developer docs:

- [Contributing](docs/dev/contributing.md) — workflow, style rules, releasing
- [Architecture](docs/dev/architecture.md) — the four layers and module map
- [Conformance strategy](docs/dev/conformance.md) — where each spec rule is enforced
- [Testing](docs/dev/testing.md) — the test suite and the load-bearing tests

The package is `zpf` (import and PyPI name); the `zipline` name on PyPI
belongs to an unrelated project.

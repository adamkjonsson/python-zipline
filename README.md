# python-zipline

`zpf` — a Python implementation of the Zipline Payload Format.

The Zipline Payload Format (`.zpf`) stores the payload of network traffic —
the bytes exchanged between endpoints once packets have been reassembled into
sessions, plus the metadata needed to consume them. Version 1.0 of the format
is specified in the [zipline repository](https://github.com/adamkjonsson/zipline).

The package is named `zpf` (not `zipline`) because the `zipline` name on PyPI
belongs to an unrelated project.

## Status

Under development; nothing released yet.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements.txt
.venv/bin/pytest                                  # run tests
ruff check .                                      # lint
.venv/bin/sphinx-build docs docs/_build/html      # build docs
.venv/bin/python -m build                         # build wheel + sdist
```

## License

MIT — see [LICENSE](LICENSE).

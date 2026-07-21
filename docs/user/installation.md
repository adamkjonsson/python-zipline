# Installation

```bash
pip install zpf
```

That is all you need to follow the [tutorial](tutorial.md). Installing the
package also puts a `zpf` command on your `PATH` — see the
[CLI reference](cli.md).

## Requirements

- **Python ≥ 3.11.**
- **Zero runtime dependencies.** `zpf` is pure Python and pulls in nothing
  else; the only third-party packages involved are development tools, and
  only if you work on the library itself.

## The name: `zpf`, not `zipline`

The import name and the PyPI package are both **`zpf`**:

```python
import zpf
```

The `zipline` name on PyPI already belongs to an unrelated project (a
backtesting library), so this implementation of the **Zipline Payload
Format** uses `zpf` — the format's file extension — everywhere the code is
named. The repository is still called `python-zipline`; only the package and
import name are `zpf`.

## Verify

```console
$ python -c "import zpf; print(zpf.__version__)"
0.1.0
$ zpf --version
zpf 0.1.0
```

## Working on `zpf` itself

Contributing to the library (rather than just using it) needs the dev tools —
pytest, ruff, sphinx, build. See [Contributing](../dev/contributing.md) for
the virtualenv setup and the quality gate.

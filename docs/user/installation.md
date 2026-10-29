# Installation

```bash
pip install "python-zipline @ git+https://github.com/adamkjonsson/python-zipline.git@v0.8.0"
```

That is all you need to follow the [tutorial](tutorial.md). Installing the
package also puts a `zpf` command on your `PATH` — see the
[CLI reference](cli.md).

```{warning}
**Do not `pip install zpf`.** The `zpf` project on PyPI is unrelated to this
library. This library is not on PyPI yet, so install a tagged release from
the repository as above. In a dependency list, name it the same way:
`python-zipline @ git+https://github.com/adamkjonsson/python-zipline.git@v0.8.0`.
```

## Requirements

- **Python ≥ 3.10.**
- **Zero runtime dependencies.** `zpf` is pure Python and pulls in nothing
  else; the only third-party packages involved are development tools, and
  only if you work on the library itself.

## Two names: `python-zipline` to install, `zpf` to import

```python
import zpf
```

The code is named **`zpf`**, the format's file extension: the import, the
command, and everything in these docs. The **distribution**, what `pip`
installs and what a dependency string names, is **`python-zipline`**, after
the repository.

They differ because neither obvious name is free on PyPI. `zipline` belongs
to an unrelated backtesting library, and `zpf` to another unrelated project.
Through `0.7.0` the distribution was named `zpf` too, so a dependency on
`zpf` could be resolved against PyPI's project instead of this one, which is
a dependency-confusion risk
([#102](https://github.com/adamkjonsson/python-zipline/issues/102)). Since
`0.8.0` it is `python-zipline`, and the import name is unchanged.

## Verify

```console
$ python -c "import zpf; print(zpf.__version__)"
0.8.0
$ zpf --version
zpf 0.8.0
$ pip show python-zipline
Name: python-zipline
...
```

## Working on `zpf` itself

Contributing to the library (rather than just using it) needs the dev tools —
pytest, ruff, sphinx, build. See [Contributing](../dev/contributing.md) for
the virtualenv setup and the quality gate.

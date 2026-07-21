# Testing

The suite is native `pytest`, in `tests/`, run with `.venv/bin/pytest`. It
mixes unit tests, three **load-bearing** tests tied directly to the spec, and
Hypothesis property tests. Every test must stay green on every change.

## Test map

| File | Covers |
| ---- | ------ |
| `test_blocks.py` | The typed block model: every registry option, the preservation rules (unknown option/block, malformed option kept raw). |
| `test_binary.py` | `BlockReader`/`BlockWriter`: framing, 4-byte alignment, the structural tier, truncation. |
| `test_jsonl.py` | The JSONL projection: spec examples, per-block field mapping, unknown-key and edge-case policy. |
| `test_golden.py` | **The golden file** — the spec's 196-byte worked example (see below). |
| `test_conformance.py` | The `ConformanceChecker` and its flat-writer (`check=True`) wiring. |
| `test_order.py` | Serial arithmetic, the streaming causal merge, and sequenced-order verification — including **the skewed worked example** (below). |
| `test_reader.py` | The session-first reader `zpf.open`: sessions, lazy records, strict/lenient modes. |
| `test_writer.py` | The ergonomic handle-based writer `zpf.create`. |
| `test_transform.py` | The merge transform and the coverage validator. |
| `test_roundtrip_property.py` | **Property tests** (below): random files round-trip; arbitrary bytes never crash the reader. |
| `test_cli.py` | The `zpf` command-line tool, driving `main()` directly. |
| `test_tutorial_examples.py` | The docs' example scripts, run for real so they can't rot. |
| `test_package.py` | Package smoke test (the version string is PEP 440). |

## The load-bearing tests

Three tests anchor the implementation to the specification itself; treat a
failure in any of them as a conformance regression, not a flaky test.

- **Golden bytes** — `test_golden.py`. Writing the spec's worked-example
  blocks must produce those exact 196 bytes, and parsing them back must yield
  the annotated field values. This pins the binary encoding down to the byte.
- **Round-trip property** — `test_roundtrip_property.py`. Over Hypothesis-
  generated files: `write → read` preserves every field; `read → re-emit` is
  byte-identical; `binary → jsonl → binary` is semantically lossless and
  stable. Also the robustness properties: an arbitrary byte string never
  makes the reader raise anything but `StructuralError`.
- **Skewed-clock merge** — `test_order.py::test_the_specs_skewed_worked_example`.
  The spec's two-tap scenario where the server's response is timestamped
  *before* the request it answers: the causal merge must still order request
  before response. This is the integration test for `order.py` +
  `transform.py`.

## Running subsets

```bash
.venv/bin/pytest                              # everything
.venv/bin/pytest tests/test_order.py          # one file
.venv/bin/pytest -k coverage                  # tests matching an expression
.venv/bin/pytest tests/test_golden.py::test_golden_file_is_196_bytes   # one test
.venv/bin/pytest -q                           # quiet; -x to stop at first failure
```

Property tests use [Hypothesis](https://hypothesis.readthedocs.io); a failing
case is shrunk and printed, and the falsifying example is replayed on the next
run from Hypothesis's local database.

## Adding a test

- Put it in the file matching the module under test (the table above).
- Follow the existing native-`pytest` style — plain `assert`, module-level
  functions, `tmp_path` for files. Tests are exempt from the docstring and
  return-annotation lint rules (see `ruff.toml`'s per-file ignores), but
  everything else in `ruff check` still applies.
- If it concerns a spec rule, prefer to phrase it against the spec's own
  example or wording, so the test documents the requirement.

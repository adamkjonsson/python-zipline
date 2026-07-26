# Validate a file

Validation reads a file and reports where it departs from the v1.0
specification. It answers three separable questions: is the file
*conformant*, is a SEQUENCED session's stored order *actually* a valid
ordering, and does a decode stage *cover* its input. This is the recipe; the
features being checked are explained in the
[provenance](../guides/provenance.md) (coverage) and
[ordering](../guides/ordering.md) (sequenced order) guides.

## On the command line

```console
$ zpf validate session.zpf
session.zpf: OK
```

A clean file exits `0`. Findings print one per line and exit `1`; a file
that can't be read at all exits `2`.

```console
$ zpf validate truncated.zpf
truncated.zpf: 1 finding(s)
truncated.zpf: truncated: stream ends inside a block's content
```

Add checks with flags:

```console
$ zpf validate merged.zpf --verify                 # also re-check sequenced order
$ zpf validate rest_decoded.zpf --input rest_raw.zpf   # also check decode coverage
$ zpf validate session.zpf --strict                # stop at the first violation
```

- `--verify` re-runs the causal check on every
  [SEQUENCED](../concepts.md#sequenced-sessions) session, confirming its
  stored order really is a valid linearization of its ordering hints.
- `--input RAW` checks the [decode-stage coverage
  guarantee](decode_stage.md#check-the-coverage-guarantee) against the raw
  file the decode stage cites.
- `--strict` escalates the first semantic violation or truncation to a hard
  error (exit `2`) instead of collecting it.

## In Python

Validation surfaces as {class}`~zpf.Diagnostic` objects on the reader. A
lenient read never raises for a nonconformant or truncated file — it collects
findings you can inspect:

```python
import zpf

with zpf.open("suspect.zpf") as reader:
    for diagnostic in reader.diagnostics:
        print(f"{diagnostic.offset}: {diagnostic.category}: {diagnostic.message}")
    ok = not reader.diagnostics
```

Verify a sequenced session's stored order explicitly with
`SessionReader.verify()`, which raises {class}`~zpf.SemanticError` if the
stored order can't be a causal linearization:

```python
with zpf.open("merged.zpf") as reader:
    for session in reader.sessions():
        if session.sequenced:
            session.verify()   # raises SemanticError on a bad order
```

Check decode coverage with {func}`zpf.check_coverage`, which returns the
violations (empty when the guarantee holds):

```python
findings = zpf.check_coverage("rest_decoded.zpf", "rest_raw.zpf")
assert findings == []
```

## Reading the diagnostics

Each finding carries a `category` you can branch on; see the full table on
the [errors page](../errors.md#working-with-diagnostic). The ones validation
produces most:

| Category | What it means | Typical fix |
| -------- | ------------- | ----------- |
| `truncated` | The stream ended inside a block. | Expected for a live capture; re-capture for a complete file. |
| `nonconformant` | A block breaks a spec MUST. | Fix the producer; the `message` names the rule. |
| `coverage-gap` | Input bytes neither decoded nor marked Undecoded. | Add a decoded span or an [Undecoded](decode_stage.md) marker. |
| `coverage-overlap` | Input bytes both decoded and marked Undecoded. | Drop the redundant Undecoded marker. |
| `coverage-excess` | A cited range runs past the input stream. | Correct the span's `off_end`. |

## Where to go next

- [Provenance](../guides/provenance.md) — the coverage guarantee these
  categories police, and what a derived file's digest buys you.
- [Errors and diagnostics](../errors.md) — the exception tiers behind these
  findings and the strict-vs-lenient distinction.
- [Write decode-stage files](decode_stage.md) — where the coverage
  categories come from.
- The [CLI reference](../cli.md#zpf-validate-file)
  for every flag and exit code.

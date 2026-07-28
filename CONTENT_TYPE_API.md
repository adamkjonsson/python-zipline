# Reading a record's payload as its `content_type` says

A plan for interpreting a record's payload according to its `content_type`
label. Companion to [`DECODER_API.md`](DECODER_API.md) (the write side) and
[`DOCS_STRUCTURE.md`](DOCS_STRUCTURE.md) (docs). Work it step by step via the
checklist at the end.

## The gap

`content_type` is carried faithfully today — written by `SessionWriter.record`
and `DecodeStage.record`, round-tripped through both faces, and its `prim:`
vocabulary is conformance-checked — but nothing *interprets* it.
`SessionReader.records()` yields `zpf.Record`, whose `payload` is always raw
`bytes`, so every consumer of a `prim:u32` record re-implements:

```python
value = int.from_bytes(record.payload, "little", signed=False)
```

…and has to know from the spec that the byte order is little-endian, that the
signedness comes from the token's `u`/`i`, and what to do when the label and
the payload length disagree. That is exactly the kind of per-consumer knowledge
the library exists to absorb.

## What the standard settles, and what it deliberately doesn't

From the spec's *Typing a decoded record* section and the `prim:` entry in
*Enums*. The governing sentence is:

> the bytes always stay the source of truth — the label never replaces them.

| Scheme | What the spec pins down | Can the library decode it unaided? |
|---|---|---|
| `prim:<tok>` | **Everything.** Closed vocabulary (`u8`…`u64`, `i8`…`i64`, `bytes`), little-endian, `u`/`i` signedness, and width MUST equal `payload_len`. | **Yes** — fully normative. |
| `mime:<type>` | That the value is an IANA media type. Nothing about turning bytes into a Python value. | **Partly**, and only by going beyond the format. |
| `dec:<token>` | Nothing — "a type **private to the record's decoder**, meaning whatever that decoder documents", namespaced by the decoder's **`name`** (not its id, not its version). | **No** — needs a caller-supplied handler. |

Two fallback rules are normative and shape the whole API:

- **An unknown scheme is opaque.** Not an error — the payload is simply bytes.
- **A `prim:` width that disagrees with `payload_len`** MUST be treated as
  unknown, and the reader "MUST NOT pad, truncate, or reinterpret".

> ⚠️ **Beyond the standard.** `prim:` decoding is entirely spec-defined. Any
> interpretation of `mime:` (text → `str`, JSON → `dict`) or `dec:` is *not* —
> the format defines those labels as advisory. This plan keeps that boundary
> visible in the API rather than blurring it: `prim:` is built in and always
> on; everything else is opt-in and caller-supplied.

## Blocker: we currently drop the records this feature must fall back on

Found while planning, and verified by writing a file with three well-framed
decoded records and reading it back. A record whose `prim:` label disagrees with
its `payload_len` is **discarded** on a lenient read, instead of surviving with
an opaque payload:

```
--- lenient read ---           # three records written, all with valid framing
records surviving: 1 (wrote 3)
  ts 1 prim:u32 b'\xd2\x04\x00\x00'
  diagnostic: nonconformant - content_type 'prim:u32' requires payload_len 4, got 5
  diagnostic: nonconformant - content_type 'prim:frobnicate' is not a legal prim: token
```

`ConformanceChecker._check_record_options` raises `SemanticError`
([conformance.py:287](src/zpf/conformance.py#L287)), and `FileReader._admit`
turns any `SemanticError` into "record dropped + diagnostic"
([reader.py:427](src/zpf/reader.py#L427)). The spec says the opposite: keep the
bytes, ignore the label. The conformance *finding* is right — a **writer** MUST
NOT emit such a record — but the reader's reaction is not, and it loses data.

This must be fixed first: a `content()` method whose documented fallback is
"you get the raw bytes" is meaningless if the record never reaches the caller.

## Proposed shape

Three layers, matching the library's existing split between the flat
spec-mirroring modules and the session-first reader.

### 1. `zpf/content.py` — the normative core

A new flat module, dependency-free and working on primitives (`bytes` + `str`),
so `blocks.py` can use it without a cycle:

```python
PRIM_WIDTHS: Mapping[str, int]                  # moved here from conformance.py

@dataclass(frozen=True)
class ContentType:
    scheme: str                                  # "prim" | "mime" | "dec" | other
    value: str
    @classmethod
    def parse(cls, label: str) -> ContentType: ...

def decode_prim(payload: bytes, token: str) -> int | bytes | None:
    """Spec-exact: little-endian, u/i signedness, width-checked. None = opaque."""
```

`decode_prim` returns `None` — not an exception — for every case the spec calls
opaque (unknown token, width mismatch), so callers implement the fallback rule
by construction.

### 2. `Record.content()` — the self-contained schemes

```python
def content(self, *, strict: bool = False) -> int | bytes:
    """The payload interpreted per ``content_type``; raw bytes when opaque."""
```

- `prim:u8`…`prim:u64` → `int`; `prim:i8`…`prim:i64` → signed `int`
- `prim:bytes`, no `content_type`, unknown scheme, bad width → `payload` unchanged
- `strict=True` → raise `ValueError` instead of falling back, for pipelines that
  must not silently accept an unusable label

**`dec:` cannot be resolved here**, and that is a spec consequence, not an
oversight: the token's namespace is the decoder's *name*, and a `Record` knows
only its `decoder_id`. Resolving that needs the file.

### 3. `FileReader.content(record)` — the file-aware entry point

The reader holds `decoders: dict[int, Decoder]`, so it can resolve
`decoder_id → Decoder.name` and dispatch `dec:` tokens through a registry:

```python
registry = zpf.ContentRegistry()
registry.register_dec("http/1.1", "request", parse_http_request)
registry.register_mime("application/json", json.loads)

with zpf.open("decoded.zpf", content=registry) as reader:
    for record in reader.session(0).records():
        print(reader.content(record))      # dec:/mime: handlers + prim: built in
```

With no registry, `reader.content(record)` is exactly `record.content()`.

### Alternatives considered

- **Put everything on `Record`.** Rejected: `dec:` would be undecodable
  forever, and stuffing a decoder-name back-reference into a frozen,
  block-faithful dataclass breaks the one-block-one-dataclass invariant.
- **Make `records()` yield a file-aware `RecordView`.** Cleanest call site
  (`record.content()` always works), but it changes the return type of the
  library's most-used method. Too invasive for the gain.

## Decisions to confirm

These change the work materially, so they are worth settling before stage 2.

1. **Return-value ambiguity.** `content()` returns `bytes` both for `prim:bytes`
   (a real, interpreted answer) and for "couldn't interpret". *Recommendation:*
   accept it for the common path and let `strict=True` disambiguate, rather than
   wrapping every result in a `Content(value, interpreted)` object. *Settled in
   stage 3 as recommended;* `strict=True` raises `ContentError` (a `ZpfError`
   **and** a `ValueError`) for every case where the answer would be a fallback.
2. **How far the built-in `mime:` handlers should go.** *Recommendation:* decode
   `mime:text/*` to `str` **only when an explicit `charset=` parameter is
   present** — never guess an encoding — and ship `application/json` as an
   opt-in handler the caller registers, not a default. Guessing is where silent
   corruption enters, and the spec is clear that bytes are the truth.
3. **Whether the `prim:` fix belongs in this plan.** It is a pre-existing
   conformance bug with standalone value; it is staged first here because the
   feature depends on it, but it could ship separately.

## Roll-out plan

Each step is independently shippable and leaves the tree green.

- [x] **1. Stop dropping records with an unusable `prim:` label.** Split
  conformance findings into *isolating* violations (today's behaviour) and
  *advisory* ones that must not cost the caller the block. A `prim:` width
  mismatch and an illegal `prim:` token become advisory: `FileReader` keeps the
  record and still reports the `nonconformant` diagnostic; `BlockWriter(check=True)`
  and the ergonomic writer still **raise**, since the writer obligation is
  unchanged. Tests in `test_conformance.py` + `test_reader.py` pinning both
  sides, and the [probe](#blocker-we-currently-drop-the-records-this-feature-must-fall-back-on)
  as a regression case.
- [x] **2. `zpf/content.py`.** `ContentType.parse`, `PRIM_WIDTHS` (moved from
  `conformance.py`, which imports it back), and `decode_prim`. Exhaustive
  round-trip tests over all 8 integer tokens × boundary values (0, ±1, min, max),
  plus every opaque case. New `tests/test_content.py`.
- [x] **3. `Record.content()`.** The `prim:` + fallback behaviour on the block
  itself, `strict=` included. Re-export nothing new at top level (it's a method).
- [x] **4. `ContentRegistry` + `FileReader.content()`.** Handler registration
  for `mime:` (by media type) and `dec:` (by decoder **name** + token, per the
  spec's namespacing), `zpf.open(..., content=...)`, and `decoder_id → name`
  resolution. Export `ContentRegistry` from `zpf`.
- [ ] **5. Optional built-in `mime:` handlers.** Per decision 2 — text with an
  explicit charset, and a documented opt-in for JSON. Every one of them marked
  in the docstring as beyond the format.
- [ ] **6. CLI (optional).** A `zpf cat --content` that renders `prim:` values
  as numbers instead of base64. Skip if the JSONL face should stay a byte-exact
  projection — worth a decision at the time.
- [ ] **7. Docs.** A "Reading payload content" section in
  [`guides/decoding.md`](docs/user/guides/decoding.md) (it already owns
  `content_type` on the write side), the `dec:`-namespace rule in
  [`concepts.md`](docs/user/concepts.md), and a how-to if the registry needs a
  worked example. (`api/content.md` landed with stage 2 — a `-W` nitpicky
  build fails on the first unresolved reference to a new module.)
  Cite the beyond-the-standard boundary explicitly.
- [ ] **8. Verify.** `.venv/bin/pytest` green, `ruff check` clean on every
  touched file, and `.venv/bin/sphinx-build -W docs docs/_build/html` clean
  (`nitpicky` is on, so a stale reference fails the build).

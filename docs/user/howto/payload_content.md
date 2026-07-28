# Read payloads as typed values

A record's `content_type` says what its payload is — `prim:u32`,
`mime:application/json`, `dec:request`. This is the recipe for reading
payloads *through* that label instead of by hand; the feature behind it, and
what the format does and does not define, is in the
[decoding guide](../guides/decoding.md#reading-payload-content).

## `prim:` needs nothing

The spec-defined scheme is built in — no setup, no registry:

```python
import zpf

with zpf.open("metrics.zpf") as reader:
    for record in reader.session(0).records():
        print(record.content_type, record.content())
        # prim:u32   -> 1234        (little-endian, unsigned)
        # prim:i16   -> -1          (signed, from the token's "i")
        # prim:bytes -> b"raw"      (the payload itself)
```

Anything the label can't honour returns the payload bytes unchanged.

## `mime:` and `dec:` need a registry

```{warning}
**Beyond the standard.** The format calls these labels advisory: it defines no
decoding for either. A handler's answer is *your* claim about the bytes, not
the format's — which is why nothing here is on by default.
```

Register handlers, pass the registry to {func}`zpf.open`, and read through
{meth}`FileReader.content <zpf.reader.FileReader.content>`:

```python
import json
import zpf

def parse_request(payload: bytes) -> dict[str, str]:
    method, target, version = payload.split(b"\r\n", 1)[0].decode().split(" ")
    return {"method": method, "target": target, "version": version}

registry = zpf.ContentRegistry()
registry.register_mime("application/json", json.loads)
registry.register_dec("http/1.1", "request", parse_request)   # decoder NAME, token

with zpf.open("rest_decoded.zpf", content=registry) as reader:
    for record in reader.session(0).records():
        value = reader.content(record)
        print(record.content_type, type(value).__name__, value)
```

```text
dec:request dict {'method': 'GET', 'target': '/index.html', 'version': 'HTTP/1.1'}
mime:application/json dict {'status': 200}
prim:u32 int 1234
```

Three rules to register by:

| Scheme | Key | Matching |
| ------ | --- | -------- |
| `mime:` | the media type | case-insensitive, parameters ignored — one `text/plain` handler serves `mime:text/plain; charset=utf-8` |
| `dec:` | the producing decoder's **`name`** plus the token | exact; `dec:request` from `http/1.1` and from `smtp` are different types |
| `prim:` | — | built in and not overridable |

## Fail instead of falling back

`content()` returns `bytes` both when the payload *is* bytes and when the
label couldn't be honoured. Pass `strict=True` to make the second case raise:

```python
try:
    value = reader.content(record, strict=True)
except zpf.ContentError as exc:      # also a ValueError
    print("unusable:", exc)          # -> content_type 'prim:u32' requires payload_len 4, got 5
```

A registered handler's own exceptions always propagate, `strict` or not — a
handler that fails is a bug or corrupt input, not a fallback.

## Without a file

{meth}`Record.content <zpf.blocks.Record.content>` works on any record you
hold, from {class}`~zpf.BlockReader` or built by hand. It covers `prim:` and
the fallback; `dec:` is the one scheme it cannot resolve, because the token is
namespaced by the decoder's *name* and a record carries only a `decoder_id` —
that lookup needs the file, hence
{meth}`FileReader.content <zpf.reader.FileReader.content>`.

## Where to go next

- [Decoding guide](../guides/decoding.md#reading-payload-content) — the feature
  in prose, both halves (labelling on write, interpreting on read).
- [Concepts: typing a payload](../concepts.md#typing-a-payload-content_type) —
  what each scheme settles, and the `dec:` namespace rule.
- [Errors and diagnostics](../errors.md#contenterror-the-label-could-not-be-honoured)
  — `ContentError`, and why an unusable label is a diagnostic rather than a
  dropped record.
- API reference: [`zpf.content`](../../api/content.md).

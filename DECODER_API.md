# Improving the API for decoders

## Possible improvments

Afters studying the tutorial for writing decoders I make the following observations:

The existing API gives detailed control, which is good, but that means that the user has to deal with a few common tasks that could be hidden behind helper functions.

It should for example be easier to create a a decoded session from a raw one, with necessary information automatically copied in.

The user is required to know quite a bit about how TCP uses ack-numbers. Ideally, the only thing a user must know about UDP is that it is packet based, and about TCP that it is stream oriented and can have known gaps.

When specifying `produced_at` in `zpf.create` there should be a helper function so that standard `datetime` objects can be used.

When consuming data in a decoder, the natural way to do that for raw UDP records and non-raw records is to simply iterate through them.
Raw TCP records, on the other hand, are different since they are part of a stream of data. The natural way to consume this data
should therefore be to iterate over joined contigous segments. This
iteration process should expose data gaps in a natural way. 

Make suggstions for how to make the API for working with decoding more ergonomic. 

## Claude's response

The friction in the current decoder path (see
[`docs/user/examples/06_write_decoder.py`](docs/user/examples/06_write_decoder.py))
falls into two clusters, and your observations map onto both:

1. **Consuming the input** — the decoder reassembles streams by hand and does
   TCP sequence arithmetic to turn wire coordinates into the logical offsets
   its spans need.
2. **Producing the derived file** — the decoder re-declares provenance,
   participants, and ids that all came straight from the input, and hand-builds
   every `Span` and `Undecoded` block.

None of this requires changing the low-level, block-faithful API; the proposals
below are all *additive* convenience layers on top of `zpf.open` / `zpf.create`.
They are ordered from the highest-value change to the smallest. Where a proposal
would let the code emit something the standard does not define, I call it out
explicitly (per the project rule on going beyond the standard).

### 1. A reassembly view for consuming input streams

This is the biggest win and it addresses two of your observations at once (the
TCP/ack knowledge, and stream-vs-packet iteration).

**Today** the decoder does this per participant:

```python
records = list(raw_session.stream(pid))
stream = b"".join(r.payload for r in records)
base = records[0].seq_start - origin        # origin = isn + 1
```

That line is doing three risky things the user shouldn't have to think about:

- it reconstructs `origin = isn + 1` and the logical-offset mapping by hand —
  exactly the "user must know how TCP numbers bytes" problem;
- `b"".join(...)` **silently welds across a gap**: if a segment is missing, the
  bytes on either side get glued together and every span computed afterwards is
  off by the size of the hole. The current example only works because its
  sample stream happens to be gapless;
- it assumes `records[0]` exists and carries a `seq_start`.

**Proposed:** a stream view obtained from the session reader that hides all of
this. `SessionReader` gains `streams()` (one view per participant) alongside the
existing low-level `stream(pid)`:

```python
@dataclass(frozen=True)
class Segment:
    """A maximal contiguous run of reassembled stream bytes."""
    data: bytes
    off_start: int        # logical offset of data[0]
    off_end: int          # == off_start + len(data)
    ts: int               # completion time of the last contributing record

@dataclass(frozen=True)
class Gap:
    """A known hole in the stream (a missing TCP segment)."""
    off_start: int
    off_end: int

class StreamView:
    participant: Participant
    off_start: int                       # first available logical offset
    def segments(self) -> Iterator[Segment]: ...   # contiguous runs only
    def chunks(self) -> Iterator[Segment | Gap]: ...  # runs and gaps, in order
    def reassembled(self) -> bytes: ...  # convenience for gapless streams;
                                         # raises if the stream has a Gap
```

The view computes offsets from `isn`/`seq_start` internally using the serial
arithmetic already in [`zpf/order.py`](src/zpf/order.py) (`record_end`,
`SEQ_SPACE`), so the user never sees a sequence number. Gaps become first-class
objects instead of a silent hazard, which is precisely the "expose data gaps in
a natural way" you asked for. A decoder loop becomes:

```python
for stream in raw_session.streams():
    for chunk in stream.chunks():
        if isinstance(chunk, zpf.Gap):
            mark_undecoded(chunk)           # honest about the hole
            continue
        for start, end, kind in split_messages(chunk.data):
            emit(stream, chunk.off_start + start, chunk.off_start + end, kind)
```

**Packet-oriented input** (raw UDP, and *any* non-raw input where records are
already whole messages) wants the other idiom — iterate records as units — so
the view offers `datagrams()`:

```python
@dataclass(frozen=True)
class Datagram:
    data: bytes
    off_start: int        # cumulative byte offset (matches check_coverage's
    off_end: int          # hint-less extent model in _stream_extents)
    ts: int
    record: Record        # escape hatch to seq_start/ack/flags if needed

for datagram in stream.datagrams():
    decode_one(datagram.data)               # one message, no reassembly
```

Under the hood `datagrams()` and `segments()` differ only in whether they join
across records; picking the right one is the *only* transport fact the user
needs — "UDP is packet based, TCP is a stream with possible gaps", exactly your
stated goal. A helper `stream.is_stream_oriented` (true when the participant has
an `isn` / `seq_start` hints) lets generic code choose automatically.

### 2. Cite input bytes directly from the stream view

A decoded record almost always cites one contiguous range of the stream it was
reading. Building that `Span` by hand means repeating the input's `source_id`,
`session_id`, and `participant_id` on every call
([`06_write_decoder.py:92-100`](docs/user/examples/06_write_decoder.py#L92-L100)).
Since the `StreamView` already knows all three, let it mint the span:

```python
span = stream.cite(off_start, off_end)      # -> zpf.Span, ids pre-filled
# or, relative to a segment you're mid-parse in:
span = segment.cite(local_start, local_end) # adds segment.off_start for you
```

`out.record(..., spans=(span,))` then reads with no id bookkeeping, and it is
impossible to accidentally cite the wrong stream.

### 3. Scaffold the derived file from its input

Everything a decode stage needs on its header and its participants is a copy of
the input: `tick_hz`, `time_epoch`, the `zpf-input` Source (with digest), and
each participant re-declared under the *same* id so spans line up. The current
example does all of this by hand, including a manual `hashlib.sha256` and a
`handles = {p.participant_id: out.participant(p.endpoint) ...}` dict that quietly
assumes participant ids are `0,1,…` in declaration order.

**Proposed:** an orchestrator that opens the input, wires the output, and hands
back a per-stream decode context:

```python
with zpf.decode_stage(
    "rest_raw.zpf", "rest_decoded.zpf",
    decoder=("http/1.1", "tutorial"),        # name, version
    produced_by="http-decode 1.0",
    produced_at=datetime(2023, 11, 14, tzinfo=UTC),   # see §5
) as dec:
    for stream in dec.streams():             # input StreamView + matching output
        for seg in stream.segments():
            for start, end, kind in split_messages(seg.data):
                dec.record(
                    stream, seg.data[start:end],
                    content_type=f"dec:http-{kind}",
                    cites=(seg.off_start + start, seg.off_start + end),
                )
```

`decode_stage` handles, once and correctly:

- computing the input digest and declaring the `zpf-input` Source;
- copying `tick_hz` / `time_epoch` onto the output header;
- re-declaring each input participant with `pid=participant.participant_id`
  explicitly (fixing the ordering assumption above), and remembering the mapping
  so `dec.record(stream, ...)` knows both the output participant handle and the
  input stream identity for the span.

For users who want to keep their own `zpf.create` call, expose the same logic as
a lower-level `writer.derive_from(reader)` that returns the source handle and the
participant map without owning the control flow.

### 4. Make the coverage guarantee hold by construction

The coverage guarantee (every input offset either cited or marked `Undecoded`)
is enforced by [`check_coverage`](src/zpf/transform.py) *after the fact*. The
decoder still has to remember to emit an `Undecoded` for every gap and every
trailing un-parsed region — the example even carries a `# nothing left
undecoded here, but be honest` branch to show how easy it is to forget.

Because the `decode_stage` context in §3 sees both what the input contains
(from the `StreamView`) and what the decoder cited (via `dec.record`), it can
close the loop automatically:

```python
# on context exit, for each input stream:
#   uncited, unmarked regions  -> Undecoded(reason="undecodable")
#   Gap regions                -> Undecoded(reason="tcp-gap")
```

so `check_coverage(output, input) == []` is guaranteed without the decoder
writing a single `Undecoded` by hand. A decoder that *wants* to say more (a
specific reason, a comment) can still call `dec.undecoded(...)` explicitly, and
the auto-fill only covers what's left.

**Standard note:** `"tcp-gap"` / `"undecodable"` are the open-vocabulary reasons
already shown in [`zpf/writer.py`](src/zpf/writer.py) and the how-to, so this
stays within the standard. The auto-fill should be *opt-in* (a flag on
`decode_stage`, default on) and must never *replace* an explicit marker — I'd
have it raise if the decoder both cites and marks the same region, mirroring the
`coverage-overlap` finding, rather than silently choosing.

### 5. Accept `datetime` for `produced_at`

Small and self-contained. `produced_at` is documented as "Unix seconds"
([`zpf/writer.py:598`](src/zpf/writer.py)), so today the user writes a magic
integer (`produced_at=1_700_000_000`). Let `create` / `decode_stage` accept a
`datetime` and convert:

```python
def _unix_seconds(value: int | datetime) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ZpfError("produced_at datetime must be timezone-aware "
                           "(use datetime.now(UTC))")
        return int(value.timestamp())
    return value
```

Requiring tz-awareness avoids the silent-local-time trap; the alternative
(assume UTC) is friendlier but surprising, so I'd raise rather than guess. The
same helper can be offered publicly as `zpf.unix_seconds(dt)` for users building
the header themselves.

### Summary

| # | Change | Observation it answers | Surface |
|---|--------|------------------------|---------|
| 1 | `SessionReader.streams()` → `StreamView` with `segments()`/`chunks()`/`datagrams()` | TCP knowledge; stream-vs-packet iteration; gaps | reader |
| 2 | `stream.cite()` / `segment.cite()` → `Span` | span boilerplate | reader |
| 3 | `zpf.decode_stage(...)` / `writer.derive_from(reader)` | "decoded session from a raw one, info copied in" | both |
| 4 | Auto-`Undecoded` for gaps and uncited regions | coverage burden | writer |
| 5 | `produced_at` accepts `datetime` | the `datetime` helper | writer |

Proposals 1–2 are independent and could ship first (pure reader additions).
Proposal 3 depends on 1; proposal 4 depends on 3. Proposal 5 is independent and
trivial. All are backward compatible: the existing handle-based path in
[`06_write_decoder.py`](docs/user/examples/06_write_decoder.py) keeps working
unchanged.

## Implementation checklist

Ordered to respect the dependencies above (5 can be done at any point).

- [ ] **1. Reassembly view (reader).** Add `Segment`, `Gap`, `Datagram`, and
  `StreamView`; expose `SessionReader.streams()` returning one view per
  participant. Compute logical offsets internally via `record_end` / `SEQ_SPACE`
  ([`zpf/order.py`](src/zpf/order.py)); surface gaps as `Gap` objects instead of
  welding across them. Implement `segments()`, `chunks()`, `datagrams()`,
  `reassembled()`, and `is_stream_oriented`.
- [ ] **2. Span helpers (reader).** Add `StreamView.cite(off_start, off_end)`
  and `Segment.cite(local_start, local_end)` returning a `zpf.Span` with
  `source_id`/`session_id`/`participant_id` pre-filled. *(Depends on 1.)*
- [ ] **3. Derive-from-input scaffolding (both).** Add `writer.derive_from(reader)`
  (copies `tick_hz`/`time_epoch`, declares the `zpf-input` Source with digest,
  re-declares participants with explicit `pid=`, returns source handle +
  participant map), then the `zpf.decode_stage(...)` orchestrator on top of it.
  *(Depends on 1.)*
- [ ] **4. Coverage auto-fill (writer).** On `decode_stage` exit, emit
  `Undecoded` for uncited regions (`reason="undecodable"`) and gaps
  (`reason="tcp-gap"`); opt-in flag (default on); raise on cite/mark overlap
  rather than override. Verify `check_coverage` returns `[]` by construction.
  *(Depends on 3.)*
- [ ] **5. `datetime` for `produced_at` (writer).** Accept `int | datetime` in
  `create` / `decode_stage`; require tz-aware datetimes; expose the conversion
  publicly as `zpf.unix_seconds(dt)`. *(Independent.)*

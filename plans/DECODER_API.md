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
[`docs/user/examples/06_write_decoder.py`](../docs/user/examples/06_write_decoder.py))
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
arithmetic already in [`zpf/order.py`](../src/zpf/order.py) (`record_end`,
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
([`06_write_decoder.py:92-100`](../docs/user/examples/06_write_decoder.py#L92-L100)).
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
is enforced by [`check_coverage`](../src/zpf/transform.py) *after the fact*. The
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
already shown in [`zpf/writer.py`](../src/zpf/writer.py) and the how-to, so this
stays within the standard. The auto-fill should be *opt-in* (a flag on
`decode_stage`, default on) and must never *replace* an explicit marker — I'd
have it raise if the decoder both cites and marks the same region, mirroring the
`coverage-overlap` finding, rather than silently choosing.

### 5. Accept `datetime` for `produced_at`

Small and self-contained. `produced_at` is documented as "Unix seconds"
([`zpf/writer.py:598`](../src/zpf/writer.py)), so today the user writes a magic
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

### 6. Preserve the input's causal order in the decoded output

*(Added after stage 3 landed, from the question "can we keep causal ordering
when the input is `sequenced`?")*

`decode_stage` currently walks one stream at a time, so the output holds every
client record, then every server record. The input's interleaving — which for a
sequenced session is a committed causal order — is thrown away.

**The order is recoverable.** `session.timeline()` gives the input's causal
order (for a sequenced input, simply its stored order — no merge). Every decoded
record cites the input bytes it came from, so each maps to the input record that
completed it. Emitting decoded records ordered by *(position of the last
contributing input record in the input timeline, then cited `off_start`)* is a
sound causal linearization: a monotone map from an order the input already
committed to. Two shapes:

- **Buffer and sort** — decode stream-by-stream as now, hold the decoded
  records, emit in timeline order at close. Simple; costs memory proportional to
  a session's decoded output.
- **Drive from the timeline** — iterate `session.timeline()` and feed bytes to
  per-stream incremental decoders, emitting units as they complete. No
  buffering, but it needs a push-style decoder interface
  (`feed(bytes) -> messages`), a much larger API change.

**The `SEQUENCED` flag is a separate, stricter question, and mostly unavailable.**
Sequencing is orthogonal to raw-vs-decoded, so a decode-stage file *may* be
sequenced. But what a sequenced session rests on is the ordering hints its
records carry, and **decoded records carry none** — no `seq_start`, no `ack`.
Per the standard a hint-less session's sequenced order *is* its timestamp order,
and `verify_sequenced` enforces exactly that:

```text
decreasing-ts causal order: REJECTED -> Record(pid 1, ts 995) steps backwards in
time (previous hint-less timestamp 1000); a hint-less sequenced order is the
timestamp order
```

That is the sting. The skewed two-sided capture in
[`test_reader.py`](../tests/test_reader.py) — response stamped *before* the request
it answers — is precisely the case ack-based sequencing exists to fix, and its
true causal order has **decreasing** timestamps. Marking such an output
`SEQUENCED` would be unverifiable and forbidden: a producer MUST NOT mark a
hint-less session sequenced unless its records share one trustworthy clock.
Decoding destroys the evidence that made the input safely sequenceable — the
input's order was clock-independent because seq/ack survived; downstream, only
timestamps do.

**Proposed:** emit causally regardless (strictly better stored order, free at
read time), and set `sequenced=True` only when the emitted timestamps come out
non-decreasing — exactly when the flag is honest for a hint-less session. That
holds when the input asserted `SINGLE_CLOCK` or came from a single tap, and
fails on skewed two-sided merges.

One structural wrinkle: `begin_session` writes the Session Descriptor *before*
any records, so the flag cannot be decided after seeing the timestamps without
also deferring session declaration. The streaming-friendly form is to make it
opt-in (`sequenced=True`) and have the stage verify as it writes, raising if the
order cannot honor it, rather than silently emitting a false assertion.

**Open question, deliberately not proposed.** When timestamps *are* skewed the
decoded file cannot convey the true order at all: it is in the stored bytes, but
with the flag off a reader re-merges by `(timestamp, pid)` and gets it wrong.
Carrying `seq_start` forward onto decoded records would fix it, but those fields
mean "sequence number of the sender's first payload byte", which a decoded
unit's payload is not — **that would go beyond the standard** and needs an
explicit decision before anyone builds it.

### Summary

| # | Change | Observation it answers | Surface |
|---|--------|------------------------|---------|
| 1 | `SessionReader.streams()` → `StreamView` with `segments()`/`chunks()`/`datagrams()` | TCP knowledge; stream-vs-packet iteration; gaps | reader |
| 2 | `stream.cite()` / `segment.cite()` → `Span` | span boilerplate | reader |
| 3 | `zpf.decode_stage(...)` / `writer.derive_from(reader)` | "decoded session from a raw one, info copied in" | both |
| 4 | Auto-`Undecoded` for gaps and uncited regions | coverage burden | writer |
| 5 | `produced_at` accepts `datetime` | the `datetime` helper | writer |
| 6 | Causal output order; conditional `SEQUENCED` | input interleaving lost by stream-at-a-time decoding | both |
| 7 | Documentation revised for stages 1–5 | docs still teach the pre-stage-1 manual path | docs |
| 8 | `decoder=` override on `DecodeStage.undecoded` | multi-decoder sessions: undecoded attribution is default-only | writer |

Proposals 1–2 are independent and could ship first (pure reader additions).
Proposal 3 depends on 1; proposal 4 depends on 3; proposal 6 depends on 3.
Proposal 5 is independent and trivial. Proposal 7 (documentation) depends on
1–5 being implemented. All are backward compatible: the existing handle-based
path in [`06_write_decoder.py`](../docs/user/examples/06_write_decoder.py) keeps
working unchanged.

## Implementation checklist

Ordered to respect the dependencies above (5 can be done at any point).

- [x] **1. Reassembly view (reader).** Add `Segment`, `Gap`, `Datagram`, and
  `StreamView`; expose `SessionReader.reassemble()` returning one view per
  participant. Compute logical offsets internally via `record_end` / `SEQ_SPACE`
  ([`zpf/order.py`](../src/zpf/order.py)); surface gaps as `Gap` objects instead of
  welding across them. Implement `segments()`, `chunks()`, `datagrams()`,
  `reassembled()`, and `is_stream_oriented`. *(Accessor named `reassemble()`,
  not `streams()`; stream-only methods raise on packet-oriented streams.)*
- [x] **2. Span helpers (reader).** Add `StreamView.cite(off_start, off_end)`
  and `Segment.cite(local_start, local_end)` returning a `zpf.Span` with
  `session_id`/`participant_id` pre-filled. *(Depends on 1.)* **Correction to
  the proposal above:** the view cannot supply `source_id`. A span's
  `source_id` names a Source in the *citing* file, so it is the id the
  decoder's own writer returns for its `zpf-input` source — the file being
  read has no way to know it. It is passed per call (`source=`) or bound once
  with the new `StreamView.cited_as(handle)`; stage 3's `derive_from` is where
  that binding becomes automatic.
- [x] **3. Derive-from-input scaffolding (both).** Add `writer.derive_from(reader)`
  (declares the `zpf-input` Source with digest, re-declares participants with
  explicit `pid=`, returns a `DerivedInput` with source handle + session writers
  + participant map), then the `zpf.decode_stage(...)` orchestrator on top of it.
  *(Depends on 1.)* **Corrections to the proposal above:**
  - `derive_from` *cannot* copy `tick_hz`/`time_epoch`: the File Header is
    written when `create()` is called, before the writer ever sees the input.
    It verifies they agree and raises otherwise; `decode_stage`, which owns the
    `create()` call, is what actually copies them.
  - `dec.record(...)` takes `ts=` explicitly (the proposal's sketch omitted it).
    The spec's timestamp rule makes record time a real decision — normally
    `seg.ts`, the completion time of the last contributing input record — so it
    is not something to guess.
  - `isn` is not copied onto the output participants: it describes the input's
    raw TCP stream, which the decoded records are no longer in.
  - Output sessions are not marked `sequenced`, since decoding stream-by-stream
    does not emit a causal order across streams.
  - Declaring the `zpf-input` Source without making the caller supply its `uri`
    and `digest` needed two small reader additions: `FileReader.path` (the path
    the file was opened from, `None` for a stream) and `FileReader.digest()`.
    `derive_from` defaults the Source's `uri` and `digest` to those, which is
    what removes the manual `hashlib` step from the caller.
- [x] **4. Coverage auto-fill (writer).** On a clean `decode_stage` close, emit
  `Undecoded` for uncited regions (`reason="skipped"`) and reassembly gaps
  (`reason="tcp-gap"`); `fill_undecoded` flag (default on); raise on cite/mark
  overlap rather than override. `check_coverage` returns `[]` by construction
  (asserted in the tests). *(Depends on 3.)* Notes:
  - **Reason for auto-filled data is `"skipped"`, not `"undecodable"`** (change
    from the proposal). `undecodable` is a strong claim — the decoder *tried and
    could not parse* — so it is reserved for an explicit `dec.undecoded(...)`
    call; auto-fill, which only knows the decoder never touched these bytes,
    says `skipped`. Both are open-vocabulary reasons the standard permits (spec:
    `undecodable`/`tcp-gap`/`truncated`/…), and both sit on the recoverable side
    (bytes exist upstream), unlike the `tcp-gap` hole.
  - The interval math (`complement`, `intersections`) that `check_coverage`
    already had was factored into `zpf/_intervals.py` and shared, so auto-fill
    and the after-the-fact check cannot drift.
  - Extent and gap ranges per stream come straight from the stage-1
    `StreamView` (`chunks()`/`datagrams()`), matching `check_coverage`'s extent
    model exactly.
  - `decode_stage` hit the 10-argument lint ceiling again, so `creator` was
    dropped from it (a derived file's producing tool is `produced_by`); callers
    wanting a distinct `creator` use the `create()` + `derive_from()` path.
  - A failing auto-fill closes the output without an End block, so a stage that
    raises stays honestly incomplete.
- [x] **5. `datetime` for `produced_at` (writer).** Accept `int | datetime` in
  `create` / `decode_stage`; require tz-aware datetimes; expose the conversion
  publicly as `zpf.unix_seconds(dt)`. *(Independent.)* The conversion lives at
  the `FileWriter.__init__` choke point, so `create`, `decode_stage`, and the
  `FileWriter` constructor all accept a datetime; the `FileHeader` block itself
  keeps `produced_at` as on-disk Unix `int`. A naive datetime raises rather than
  being read as local time.
- [ ] **6. Causal output order (both).** Order a session's decoded records by the
  input's `timeline()` — keyed on the last contributing input record, ties broken
  by cited `off_start` — instead of emitting stream-by-stream. Start with the
  buffer-and-sort shape; the streaming form needs a push-style decoder interface
  and is out of scope. Then add opt-in `sequenced=True`, verified while writing:
  decoded records are hint-less, so the stage must confirm the emitted timestamps
  are non-decreasing and raise rather than assert a sequenced order it cannot
  honor. Note `begin_session` writes the Session Descriptor before any records,
  so the flag cannot be inferred after the fact without deferring session
  declaration. **Do not** carry `seq_start` onto decoded records to work around
  the skewed-clock case without an explicit decision — it goes beyond the
  standard. *(Depends on 3.)*
- [x] **7. Revise the documentation for stages 1–5 (docs).** The prose, examples,
  and API reference still teach the pre-stage-1 manual path; bring them up to
  date with everything shipped. *(Depends on 1–5; **excludes stage 6**, which is
  not yet implemented — do not document causal ordering or a `sequenced=` option
  for decode stages.)* One deviation from the scope below: the `produced_at`
  datetime note landed in the decode-stage how-to and the API docstrings rather
  than `concepts.md`, which does not discuss header fields at all — forcing it
  there would have been the wrong altitude. Scope:
  - **API reference.** Add autodoc pages `docs/api/reassembly.md` (`zpf.reassembly`)
    and `docs/api/decode.md` (`zpf.decode`), and list them in the
    [`docs/api/index.md`](../docs/api/index.md) toctree. `zpf/_intervals.py` is
    private — leave it out. The other pages are `automodule`, so the new public
    members on existing modules (`SessionReader.reassemble`, `FileReader.path` /
    `digest`, `FileWriter.derive_from`, `DerivedInput`, `unix_seconds`) surface
    once they have docstrings (they do) — just verify the build.
  - **Decoder tutorial & how-to.** Rewrite
    [`docs/user/tutorial-decoding.md`](../docs/user/tutorial-decoding.md) and
    [`docs/user/howto/decode_stage.md`](../docs/user/howto/decode_stage.md) around
    `zpf.decode_stage` + `reassemble()`/`segments()` + `cite()`, replacing the
    hand-rolled `hashlib`, participant-map dict, and manual-`Undecoded` steps.
    Show `fill_undecoded` covering the rest by construction, and the
    `skipped` vs `tcp-gap` vs explicit-`undecodable` reason distinction.
  - **Examples.** Update [`05_rest_raw_input.py`](../docs/user/examples/05_rest_transport_input.py)
    (since renamed `05_rest_transport_input.py` by the 0.16 port),
    [`06_write_decoder.py`](../docs/user/examples/06_write_decoder.py), and
    [`07_coverage.py`](../docs/user/examples/07_coverage.py) to the ergonomic path.
    These are executed verbatim by
    [`tests/test_tutorial_examples.py`](../tests/test_tutorial_examples.py) and
    `{literalinclude}`-d into the tutorials, so keep them runnable and update
    that test's expected-output assertions in lockstep.
  - **Concepts.** Note in [`docs/user/concepts.md`](../docs/user/concepts.md) that
    `produced_at` accepts a `datetime`, and that the reassembly view is the
    intended way to consume input streams.
  - **Verify.** `.venv/bin/sphinx-build docs docs/_build/html` clean (no new
    warnings) and `.venv/bin/pytest` green, since the example scripts are
    tested. Keep the note that the low-level handle-based path still works.
- [x] **8. `decoder=` override on `DecodeStage.undecoded` (writer).** Multiple
  decoders per session are already fully supported — the format keys `decoder_id`
  per record, `FileWriter.add_decoder` can be called repeatedly, and
  `DecodeStage.record` takes a per-record `decoder=` override (declare extras via
  `dec.writer.add_decoder(...)`). The one asymmetry was attribution of
  *undecoded* regions: `DecodeStage.undecoded` and the `fill_undecoded` auto-fill
  always attributed their `Undecoded` blocks to the stage's default decoder.
  Added a `decoder: DecoderHandle | None = None` override to
  `DecodeStage.undecoded` (defaulting to `self._decoder`), mirroring `record()`,
  so a region can be attributed to the decoder that actually declined it. Auto-fill
  stays on the default decoder — it has no basis to pick a specific one — and the
  docstring says so. *(Depends on 3–4.)*

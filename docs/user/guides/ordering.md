# Ordering

A `.zpf` answers not just *what* the endpoints said but *in what order*. That
sounds like it should be a timestamp sort — and often it is — but when the two
directions of a conversation are captured on separate, skewed clocks, sorting by
timestamp can place an answer before the question it answers. This guide
explains how `zpf` recovers the true order and how you consume and produce it.
For the normative model see
[Concepts](../concepts.md#ordering-why-timestamps-are-not-enough).

## Why timestamps aren't enough

TCP's sequence and acknowledgement numbers give a clock-independent fix: a
segment from B carrying `ack = N` proves B had already received A's stream up to
byte `N` when it was built — a causal *happens-before* edge that holds whatever
either clock says. Records carry the wire's absolute `seq_start`/`ack` values as
**ordering hints**, from which a consumer derives a partial order (a DAG of
causal edges) and uses timestamps only to break ties between genuinely
concurrent records. Because each participant's records are stored already sorted
by `seq_start`, resolving the order is a cheap streaming merge, never a sort.

## Consuming the order: three levels

Pick the level of involvement you need:

- {meth}`~zpf.SessionReader.timeline` — the one you usually want. It yields a
  session's records in causal order, running the merge only when the session
  isn't already sequenced (below). It works the same either way, so you don't
  have to know which.
- {meth}`~zpf.SessionReader.stream` — one participant's records in stream order,
  no merge. What a decoder reads (via {meth}`~zpf.SessionReader.reassemble`).
- {func}`~zpf.causal_merge` and friends in {mod}`zpf.order` — the raw partial
  order, if you need the DAG itself rather than a linearization.

```python
with zpf.open("cap.zpf") as reader:
    for session in reader.sessions():
        for record in session.timeline():   # causal order, skew or not
            print(record.timestamp, record.sender_pid, record.payload[:20])
```

## Sequenced sessions

Files are read far more often than written, so a producer that has already
resolved the order can **bake it in**: store the records so that file order is a
valid causal linearization, and set the **`SEQUENCED`** flag on the session.
Readers then consume records in stored order and skip the merge entirely —
`timeline()` simply returns the stored order.

The flag is **per session** (a file may mix sequenced and unsequenced sessions),
and the `seq_start`/`ack` hints stay present, so the stored order remains
*checkable*. Confirm it with {meth}`~zpf.SessionReader.verify` (or
`zpf validate --verify`), which raises {class}`~zpf.SemanticError` if the stored
order could not be a causal linearization:

```python
with zpf.open("merged.zpf") as reader:
    for session in reader.sessions():
        if session.sequenced:
            session.verify()   # raises on an order that isn't causal
```

## When timestamps are the only order

A session with no `seq/ack` hints — a chat room, a one-way UDP feed — has no
causal edges at all, so its order rests purely on timestamps. That is sound only
when every record was stamped against **one trustworthy clock** (a single
observer saw the whole session). A producer must therefore not mark a hint-less
session `SEQUENCED` without that guarantee; the file-wide form is the
`SINGLE_CLOCK` flag (`zpf.create(..., single_clock=True)`), asserting every
record in the file shares one clock. See
[Concepts](../concepts.md#ordering-why-timestamps-are-not-enough) for the full
rule.

## Merging separately-captured directions

The canonical reason the order needs resolving is the **two-tap** capture: each
direction recorded at its own vantage point, in its own file, on its own clock.
{func}`zpf.merge_files` interleaves the two into their true causal order once and
writes a single `SEQUENCED` [pass-through](provenance.md) file, so every
downstream reader replays the stored order and never pays for ordering again —
the write-side counterpart to `timeline()` on the read side. The
[merge how-to](../howto/merge.md) is the recipe; tutorial
[stage 3](../tutorial.md#3-causal-order) motivates the problem with a worked
skew.

## See also

- [Concepts: ordering](../concepts.md#ordering-why-timestamps-are-not-enough)
  and [sequenced sessions](../concepts.md#sequenced-sessions) — the normative
  model.
- [Merge two captured directions](../howto/merge.md) — the task recipe, and
  `zpf validate --verify` to re-check a stored order.
- API reference: {mod}`zpf.order` (the merge machinery,
  {func}`~zpf.causal_merge`, {func}`~zpf.verify_sequenced`) and
  [`zpf.transform`](../../api/transform.md) ({func}`~zpf.merge_files`).

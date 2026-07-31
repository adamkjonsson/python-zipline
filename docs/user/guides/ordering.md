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

## Timestamps are not an ordering invariant

Worth stating plainly, because it is the rule most readers get wrong: stored
timestamps **may run backwards**, in any session, sequenced or not. That is an
expected consequence of skewed capture clocks and of causal sequencing — the
[worked skew](../tutorial.md#3-causal-order) stores a record stamped `995` after
the one at `1000` that caused it — not a corruption signal. So a reader must not
reject a file, discard a session, or re-sort a sequenced session because of it.

Timestamps order records in exactly one place: as the tie-break between causally
*concurrent* records during a merge. Ties there are broken by
`(timestamp, participant_id)`, and `participant_id` is unique within its session,
so the merge is fully deterministic — every reader of the same file computes the
same interleaving. The merge is also **stable**: one participant's own records
keep their stored order whatever their stamps do.

## What a hint-less sequenced session rests on

A session with no `seq/ack` hints — a chat room, a one-way UDP feed — has no
causal edges at all, so its order rests on something the file does not otherwise
record. A producer must therefore not mark such a session `SEQUENCED` without a
sound basis, and **must say what that basis is** in `sequenced_basis`:

| Value | The order rests on |
|-------|--------------------|
| `clock` | one trustworthy clock shared by every record — the `SINGLE_CLOCK` case |
| `protocol` | ordering carried by the protocol itself, e.g. a server-assigned sequence |
| `external` | an order the producer knows out of band |
| `trivial` | nothing to get wrong — one participant, or only one that ever sends |

```python
with zpf.create("chat.zpf", tick_hz=1_000_000) as w:
    w.add_source("capture", uri="chat.pcap")
    with w.begin_session(proto="irc", sequenced=True,
                         sequenced_basis="clock") as session:
        ...
```

Recording it is unconditional — `trivial` exists so a producer with nothing to
get wrong still names what it relied on. Omitting it on a hint-less sequenced
session is a semantic violation, and the writer refuses it.

Note *when* that can be judged: whether a session is hint-less is a property of
its **records**, and declare-on-first-use puts the Session Descriptor before
them. A reader therefore concludes it only at Session End or end-of-stream,
which is where {class}`~zpf.ConformanceChecker` defers the check. The producer
needs no such deferral — it decides by what it is relying on, which it knows the
moment it sets the flag.

The file-wide `SINGLE_CLOCK` flag (`zpf.create(..., single_clock=True)`) asserts
every record in the file shares one clock, and supplies the `clock` basis for
every hint-less session in it.

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

# High level plan

Here's the high-level plan, built from the full spec read and your four answers (everything in scope; two public writer layers; session-first reader; `zpf` naming).

## What we're building

A Python package `zpf` (import and PyPI name — `zipline` is Quantopian's trading library, so CLAUDE.md's src/zipline/ line should be updated to src/zpf/) that is a complete, conformant implementation of ZPF v1.0: the binary container, the JSONL projection, all three file kinds (raw, decode-stage, pass-through), the consumer-side causal merge, plus a CLI and the transform tools.

## API shape (the part we start with)

The library is four layers, each public, each built on the one below. The ergonomics goal: the easy path writes only conformant files and reads any conformant file, while tools that need exact block control drop one layer down.

**Layer 1 — blocks**. Typed frozen dataclasses mirroring the spec 1:1: FileHeader, Source, Decoder, Session, Participant, SessionEnd, Record, Undecoded, NameResolution, End, Custom, plus Span, Origin, enums for flags/kinds/roles. Every block preserves unrecognized options and unknown blocks round-trip byte-faithfully — the spec makes preservation a MUST, so it's designed in at the bottom, not bolted on.

**Layer 2 — block I/O**. BlockReader/BlockWriter over the binary frame (little-endian, 4-byte alignment, TLV padding), and a zpf.jsonl module implementing the projection with the same dataclasses. The two error tiers from the spec become the exception hierarchy: StructuralError (always reject), SemanticError (isolate; reader collects diagnostics), Truncated (expected condition, prior blocks stay valid).

**Layer 3 — ergonomic reader/writer**. The front doors:

```
with zpf.create("out.zpf", tick_hz=1_000_000) as w:      # writer: handles carry ids
    s = w.begin_session(proto="tcp", key="…")
    a = s.participant(endpoint="10.0.0.1:51000", isn=1000)
    s.record(sender=a, ts=1000, payload=b"…", seq_start=1001, ack=5001)
    s.end(reason="fin")                                    # End block on clean close
```

```
with zpf.open("cap.zpf") as f:                             # reader: session-first
    for session in f.sessions():
        for rec in session.timeline():                     # causal order, always
            ...
```

The writer enforces declare-on-first-use, per-participant seq_start order, one-Session-End, and file-kind purity (raw vs decode-stage vs pass-through) so misuse fails at write time, not in some other tool's reader. timeline() consumes stored order when the session is SEQUENCED and otherwise runs the streaming k-way merge transparently — the caller never knows which. A lower-level zpf.events(...) iterator serves pipes and live tails in bounded memory.

**Layer 4 — transforms + CLI**. The merge transform (sideA.zpf + sideB.zpf → sequenced merged.zpf), decode-stage writer helpers with a coverage-guarantee checker (every input offset decoded or marked Undecoded), and a zpf console script: cat (→ JSONL), convert (JSONL↔binary), validate, info, merge.

## Milestones

1. **Scaffolding** — pyproject.toml, venv, requirements.txt, empty src/zpf/, pytest + ruff green, fix CLAUDE.md layout references.
2. **Blocks + binary I/O** — the spec's 196-byte worked example becomes the golden test: we must produce and parse those exact bytes.
3. **JSONL projection** — property test: binary → JSONL → binary preserves every field value (spec says semantic, not byte-exact, equality).
4. **Writers** (both layers) with conformance enforcement.
5. **Readers** — events, session view, strict/lenient validation modes, truncation.
6. **Causal ordering** — RFC 1982 serial arithmetic, streaming merge; the spec's skewed two-file example (server timestamp before the client request it answers) is the integration test.
7. **Transforms, CLI, Sphinx docs, wheel**.

Ordering rationale: blocks-first isn't at odds with your "start at the API end" instinct — the API sketch above is the design commitment, decided now; milestones 2–3 just build the foundation it needs, and milestone 4 is where the sketch becomes code and we iterate on feel.

## Proposed defaults (flag now if you disagree)
- Python ≥ 3.11, stdlib-only runtime (zero dependencies); dev deps: pytest, ruff, sphinx, build, and hypothesis for the round-trip property tests.
- The spec's own open questions (compression, per-session digests) stay out of scope — v1.0 is final and doesn't include them.
- Repo name stays `python-zipline`; only package/import become `zpf`.

One small thing I noticed for later: CLAUDE.md says docs live in src/docs but the build command uses docs/ — worth reconciling when we scaffold.

If this matches what you had in mind, I'd start with milestone 1 (scaffolding) and the block layer.
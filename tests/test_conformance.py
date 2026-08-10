"""Tests for the semantic conformance checker and its flat-writer wiring."""

from __future__ import annotations

import io

import pytest
from test_golden import GOLDEN_BLOCKS
from test_jsonl import DECODED_EXAMPLE, MERGED_EXAMPLE

import zpf
from zpf.blocks import Origin, Span

HEADER = zpf.FileHeader(tick_hz=1)
DERIVED_HEADER = zpf.FileHeader(tick_hz=1, produced_by="tool 1.0", produced_at=1_719_500_000)
CAP = zpf.Source(source_id=1, kind=zpf.SourceKind.CAPTURE)
INP = zpf.Source(source_id=2, kind=zpf.SourceKind.ZPF_INPUT)
DEC = zpf.Decoder(decoder_id=3, name="http/1.1")
SESS = zpf.Session(session_id=5)
PART = zpf.Participant(session_id=5, participant_id=0)
ORIGIN = Origin(source_id=2, session_id=9, participant_id=0)


def raw_record(**kwargs: object) -> zpf.Record:
    args: dict = {
        "session_id": 5, "sender_pid": 0, "source_id": 1, "timestamp": 0, "payload": b"x",
    }
    args.update(kwargs)
    return zpf.Record(**args)


def accept(*blocks: zpf.Block) -> zpf.ConformanceChecker:
    """Feed a stream without finalizing it.

    Deliberately mid-stream: several tests assert that a deferred rule has
    *not* fired yet, then drive Session End or :meth:`finish` themselves.
    Use :func:`finished` to check a whole file.
    """
    checker = zpf.ConformanceChecker()
    checker.check(blocks)
    return checker


def reject(*blocks: zpf.Block, match: str) -> None:
    """Assert the sequence's first finding isolates its block.

    Finalizes, because since 0.16 a violation may only be decidable at the
    end: both axes are properties of a *stream*, so the per-participant
    rules wait until a participant's records are all in — Session End, or
    end-of-stream for a session that never got one. A mid-stream violation
    still raises where it always did.
    """
    with pytest.raises(zpf.SemanticError, match=match) as caught:
        finished(*blocks)
    assert not isinstance(caught.value, zpf.AdvisoryError)


def advise(*blocks: zpf.Block, match: str) -> zpf.ConformanceChecker:
    """Assert the sequence's first finding is advisory, and return the checker.

    The checker is returned mid-stream (the advisory raise interrupted
    :meth:`check`) so a caller can assert it absorbed the reported block.
    """
    checker = zpf.ConformanceChecker()
    with pytest.raises(zpf.AdvisoryError, match=match):
        checker.check(blocks)
    return checker


RAW_PRELUDE = (HEADER, CAP, SESS, PART)


# --- Declare-before-use and id uniqueness --------------------------------------


def test_references_must_be_declared():
    reject(HEADER, CAP, raw_record(), match="undeclared session")
    reject(HEADER, CAP, SESS, raw_record(), match="undeclared sender")
    reject(HEADER, SESS, PART, raw_record(), match="undeclared source")
    reject(*RAW_PRELUDE, raw_record(decoder_id=9), match="undeclared decoder")
    reject(HEADER, zpf.Participant(session_id=5, participant_id=0), match="undeclared session")
    reject(HEADER, zpf.SessionEnd(session_id=5), match="undeclared session")
    reject(HEADER, zpf.NameResolution(session_id=5, participant_id=0), match="undeclared session")
    reject(
        HEADER, SESS, zpf.NameResolution(session_id=5, participant_id=1),
        match="undeclared participant",
    )
    reject(
        DERIVED_HEADER,
        zpf.Undecoded(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1),
        match="undeclared source",
    )


def test_ids_must_not_be_reused():
    reject(HEADER, CAP, CAP, match="source id 1 declared twice")
    reject(HEADER, DEC, DEC, match="decoder id 3 declared twice")
    reject(HEADER, SESS, SESS, match="session id 5 declared twice")
    reject(HEADER, SESS, zpf.SessionEnd(session_id=5), SESS, match="declared twice")
    reject(HEADER, SESS, PART, PART, match="participant 0 of session 5 declared twice")


def test_first_block_must_be_a_header_and_only_one():
    reject(SESS, match="first block must be a File Header")
    reject(HEADER, HEADER, match="second File Header")


# --- Session lifetime -----------------------------------------------------------


def test_nothing_after_session_end():
    end = zpf.SessionEnd(session_id=5)
    reject(*RAW_PRELUDE, end, end, match="after its Session End")
    reject(*RAW_PRELUDE, end, raw_record(), match="after its Session End")
    reject(
        *RAW_PRELUDE, end, zpf.Participant(session_id=5, participant_id=1),
        match="after its Session End",
    )
    reject(
        *RAW_PRELUDE, end, zpf.NameResolution(session_id=5, participant_id=0),
        match="after its Session End",
    )


def test_nothing_after_the_end_block():
    reject(HEADER, zpf.End(), SESS, match="after the End block")


# --- Per-participant record order ------------------------------------------------


def test_seq_start_order_is_enforced():
    reject(
        *RAW_PRELUDE, raw_record(seq_start=100), raw_record(seq_start=50),
        match="seq_start order",
    )
    accept(*RAW_PRELUDE, raw_record(seq_start=50), raw_record(seq_start=50))  # SYN-style tie
    accept(*RAW_PRELUDE, raw_record(seq_start=0xFFFF_FFF0), raw_record(seq_start=0x10))  # wrap
    reject(
        *RAW_PRELUDE, raw_record(seq_start=0x10), raw_record(seq_start=0xFFFF_FFF0),
        match="seq_start order",
    )
    accept(*RAW_PRELUDE, raw_record(), raw_record())  # hint-less records unconstrained


def test_order_is_per_participant():
    other = zpf.Participant(session_id=5, participant_id=1)
    accept(
        *RAW_PRELUDE, other,
        raw_record(seq_start=100),
        raw_record(sender_pid=1, seq_start=50),  # different stream, no constraint
    )


# --- File-kind purity --------------------------------------------------------------


def test_one_participant_may_not_mix_layers():
    decoded = raw_record(
        source_id=2, decoder_id=3,
        spans=(Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1),),
    )
    reject(
        DERIVED_HEADER, CAP, INP, DEC, SESS, PART,
        raw_record(),  # no decoder: transport by the layer rule
        decoded,  # a decoder declaring `decoded`
        match="resolves to two layers",
    )
    # Order does not matter: the rule is about the set of layers a stream
    # resolves to, not about which record arrived first.
    reject(
        DERIVED_HEADER, CAP, INP, DEC, SESS, PART,
        decoded,
        raw_record(),
        match="resolves to two layers",
    )


def test_a_capture_sourced_record_may_carry_a_decoder_id():
    """0.15 legalised both capture-sourced shapes, and each has a vector.

    A decoded stream with no predecessor file — a TLS-terminating proxy,
    an ``SSL_write`` uprobe — is ``proxy-decoded``. A head-of-pipeline
    reassembler declaring itself, with ``output_layer = transport``, is
    ``reassembler-declared``. The retired rule inferred the layer from
    ``decoder_id`` and the provenance from the layer; both steps were wrong.
    """
    accept(HEADER, CAP, DEC, SESS, PART, raw_record(source_id=1, decoder_id=3))


def test_an_undecoded_block_rides_beside_a_capture_sourced_stream():
    """No longer a file-kind conflict, because there is no file kind.

    An Undecoded block names an *input*; a capture-sourced record names a
    capture. Both statements are about different streams and 0.15 stopped
    them contradicting each other — which is what ``mixed-derivation``
    ships. The block naming a ``capture`` Source is still refused here;
    that reading is Phase 5's.
    """
    undecoded = zpf.Undecoded(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=4)
    finished(DERIVED_HEADER, CAP, INP, SESS, PART, raw_record(), undecoded)
    accept(DERIVED_HEADER, INP, DEC, undecoded)


# --- Undecoded against a capture Source (0.16) -----------------------------------------


def against_capture(**kwargs: object) -> zpf.Undecoded:
    args: dict = {
        "source_id": 1, "session_id": 0, "participant_id": 0,
        "off_start": 4096, "off_end": 4396, "reason": "overlap-discarded",
        "reason_class": "bytes",
    }
    args.update(kwargs)
    return zpf.Undecoded(**args)


def test_a_reassembler_may_declare_an_overlap_it_discarded():
    """``undecoded-in-capture``: legal, and it needs no derived header.

    Reassembly *is* a transform, and a destructive one. What the block adds
    here is bytes that are in the capture and did not reach the output —
    an overlapping retransmit the reassembler dropped, which nothing else in
    the file can express.
    """
    finished(HEADER, CAP, SESS, PART, raw_record(), against_capture())


def test_a_capture_sourced_undecoded_has_no_id_namespace():
    """One struct, one rule: the body is read by the source's kind.

    A capture has no `.zpf` inside it, so there are no ids to name and the
    offsets are byte offsets into the capture file.
    """
    reject(
        HEADER, CAP, SESS, PART, raw_record(), against_capture(session_id=7),
        match="unused and MUST be written 0",
    )
    reject(
        HEADER, CAP, SESS, PART, raw_record(), against_capture(participant_id=1),
        match="unused and MUST be written 0",
    )


def test_only_the_bytes_exist_class_is_available_against_a_capture():
    """``isolate-hole-against-capture``, and why the bar is not about layers.

    A hole needs no block there: the reassembled stream is a transport
    layer whose hole-inclusive offsets already carry the gap, and the
    sequence numbers already carry its extent. Declaring it again is a
    second account of the same missing bytes with no rule for which to
    believe — the contradiction that also bars a Discontinuity from such a
    stream.
    """
    for reason in ("gap", "truncated"):
        reject(
            HEADER, CAP, SESS, PART, raw_record(),
            against_capture(reason=reason, reason_class=None),
            match="only the bytes-exist class is available",
        )
    # A non-canonical reason declaring the hole class is caught the same way:
    # the class is what decides, not the word.
    reject(
        HEADER, CAP, SESS, PART, raw_record(),
        against_capture(reason="never-captured", reason_class="hole"),
        match="only the bytes-exist class is available",
    )


def test_a_capture_sourced_undecoded_discharges_no_coverage_obligation():
    """Purely declarative: it neither satisfies a guarantee nor creates one.

    The guarantee is scoped within each input participant stream, and a
    capture has none. Counting the block would invent a stream whose only
    covered range is the block itself, and report everything below it as an
    unaccounted hole.
    """
    checker = finished(HEADER, CAP, SESS, PART, raw_record(), against_capture())
    assert checker.coverage_findings() == []


def undecoded(**kwargs: object) -> zpf.Undecoded:
    args: dict = {
        "source_id": 2, "session_id": 9, "participant_id": 0, "off_start": 0, "off_end": 4,
    }
    args.update(kwargs)
    return zpf.Undecoded(**args)


def hintless_session(**kwargs: object) -> list[zpf.Block]:
    """Build a sequenced session whose records carry no seq/ack."""
    args: dict = {"session_id": 5, "flags": zpf.SessionFlags.SEQUENCED}
    args.update(kwargs)
    return [HEADER, CAP, zpf.Session(**args), PART, raw_record()]


def finished(*blocks: zpf.Block) -> zpf.ConformanceChecker:
    """Check a whole file, including the end-of-stream pass."""
    checker = accept(*blocks)
    checker.finish()
    return checker


def test_a_hintless_sequenced_session_must_record_its_basis():
    # The rule cannot fire when the Session Descriptor is read: whether the
    # session is hint-less is a property of its *records*, and
    # declare-on-first-use puts the descriptor before them.
    checker = accept(*hintless_session())  # nothing raised yet
    with pytest.raises(zpf.SemanticError, match="sequenced_basis"):
        checker.finish()


def test_the_basis_requirement_is_settled_at_session_end():
    checker = accept(*hintless_session())
    with pytest.raises(zpf.SemanticError, match="sequenced_basis"):
        checker.observe(zpf.SessionEnd(session_id=5))


def test_any_hint_anywhere_means_the_session_is_not_hintless():
    # One hint yields causal edges, so the order rests on something the file
    # already records and no basis is owed. An ack alone counts.
    finished(HEADER, CAP, zpf.Session(session_id=5, flags=zpf.SessionFlags.SEQUENCED),
             PART, raw_record(seq_start=1000))
    finished(HEADER, CAP, zpf.Session(session_id=5, flags=zpf.SessionFlags.SEQUENCED),
             PART, raw_record(ack=1000))


def test_a_basis_satisfies_the_requirement():
    for basis in sorted(zpf.SEQUENCED_BASES):
        finished(*hintless_session(sequenced_basis=basis))


def test_an_unsequenced_session_owes_no_basis():
    finished(HEADER, CAP, zpf.Session(session_id=5), PART, raw_record())


def test_a_canonical_reason_implies_its_class():
    # The canonical four need no reason_class, and each sits in a fixed class.
    for reason in ("undecodable", "skipped", "gap", "truncated"):
        accept(DERIVED_HEADER, INP, DEC, undecoded(reason=reason))
    assert zpf.UNDECODED_REASONS["skipped"] == "bytes"  # intent differs, class does not
    assert zpf.UNDECODED_REASONS["undecodable"] == "bytes"
    assert zpf.UNDECODED_REASONS["gap"] == "hole"


def test_a_non_canonical_reason_must_name_its_class():
    # The vocabulary is open so a producer can be specific about *how*; that
    # freedom must not cost the consumer the one fact it acts on.
    reject(
        DERIVED_HEADER, INP, DEC, undecoded(reason="rtp-seq-gap"),
        match="must carry reason_class",
    )
    accept(DERIVED_HEADER, INP, DEC, undecoded(reason="rtp-seq-gap", reason_class="hole"))


def test_reason_class_must_be_one_of_the_two_classes():
    reject(
        DERIVED_HEADER, INP, DEC, undecoded(reason="rtp-seq-gap", reason_class="maybe"),
        match="'bytes' or 'hole'",
    )


def test_reason_class_must_agree_with_a_canonical_reason():
    # Redundant but permitted — so long as it does not contradict the table.
    accept(DERIVED_HEADER, INP, DEC, undecoded(reason="gap", reason_class="hole"))
    reject(
        DERIVED_HEADER, INP, DEC, undecoded(reason="gap", reason_class="bytes"),
        match="reason_class says",
    )


def test_recoverability_is_unknown_without_a_class():
    # A consumer must not guess, least of all "hole", which would discard
    # bytes that may well exist.
    assert undecoded(reason="gap").recoverability == "hole"
    assert undecoded(reason="skipped").recoverability == "bytes"
    assert undecoded(reason="rtp-seq-gap", reason_class="hole").recoverability == "hole"
    assert undecoded(reason="rtp-seq-gap").recoverability is None


def test_pass_through_records_carry_no_spans():
    # spans versus origin *is* the discriminator, so a record carrying spans
    # in a file whose participants carry origin is a kind conflict rather
    # than a rule of its own.
    span = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1)
    reject(
        DERIVED_HEADER, INP, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=ORIGIN),
        raw_record(source_id=2, spans=(span,)),
        match="carries origin and holds records carrying spans",
    )


def test_decoder_id_decides_neither_axis():
    # A pass-through preserving a decoded layer: records keep decoder_id and
    # content_type but carry no spans, provenance is the participants'
    # origin, and inherited Undecoded blocks ride along. 0.9 could not
    # express this, and a strict 0.9 reader refuses it.
    finished(
        DERIVED_HEADER, INP, DEC, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=ORIGIN),
        raw_record(source_id=2, decoder_id=3, content_type="dec:request"),
        zpf.Undecoded(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=4),
    )


def test_a_records_spans_name_a_zpf_input_source():
    """Keyed on carrying ``spans``, not on carrying a ``decoder_id``.

    A record with spans is a decode stage's output, and §Conformance
    requires those to reference a ``zpf-input`` Source. It used to be keyed
    on ``decoder_id``, which asked for a *capture* span from a record
    without one — an inference the two-axis model removed.

    The Undecoded block's body is the same packed shape and is deliberately
    not held to this: it may name a capture, which is Phase 5's reading.
    """
    input_span = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1)
    capture_span = Span(source_id=1, session_id=0, participant_id=0, off_start=0, off_end=1)
    accept(DERIVED_HEADER, INP, DEC, SESS, PART, raw_record(source_id=2, spans=(input_span,)))
    reject(
        DERIVED_HEADER, INP, DEC, CAP, SESS, PART,
        raw_record(source_id=2, decoder_id=3, spans=(capture_span,)),
        match="ZPF_INPUT source",
    )


# --- Pass-through origins ------------------------------------------------------------


def test_origin_on_a_capture_sourced_stream_is_a_violation():
    """Binds per stream, so the origin and the capture record must be one.

    Under the file-wide rule these could sit on *different* participants
    and still conflict. They cannot now, and should not: a file may hold a
    preserved stream beside a captured one. What stays forbidden is one
    stream claiming both — a capture-sourced stream's ``source_id`` is the
    whole of its provenance.
    """
    reject(
        DERIVED_HEADER, CAP, INP, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=ORIGIN),
        raw_record(source_id=1),
        match="carries origin, but its records are capture-sourced",
    )


def test_a_zpf_sourced_stream_must_be_created_or_preserved():
    """Neither is a violation of its own since 0.16, and it binds per stream.

    A participant with no ``origin`` whose records carry no ``spans`` names
    no provenance for its bytes at all: nothing resolves one level down and
    no coverage obligation can be computed in either direction. It used to
    be reachable only as a file-kind conflict, which meant a file with one
    such stream and nothing else to conflict with passed.
    """
    reject(
        DERIVED_HEADER, INP, SESS, PART,
        raw_record(source_id=2),
        match="neither origin nor records with spans",
    )
    # It binds on zpf-sourced streams alone: a capture-sourced participant
    # correctly carries neither, and its source_id is the whole of its
    # provenance.
    finished(HEADER, CAP, SESS, PART, raw_record())


def test_origin_must_reference_a_zpf_input_source():
    bad_origin = Origin(source_id=1, session_id=9, participant_id=0)
    reject(
        DERIVED_HEADER, CAP, INP, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=bad_origin),
        match="zpf-input",
    )


# --- Derived header, reserved bits, prim widths ----------------------------------------


def test_derived_files_must_declare_their_provenance():
    reject(
        HEADER, INP, DEC, SESS, PART,
        raw_record(source_id=2, decoder_id=3),
        match="produced_by and produced_at",
    )


def test_reserved_flag_bits_are_not_a_violation():
    # The specification groups a nonzero reserved field with unknown block
    # types and unknown option ids: part of the extension mechanism, "not a
    # violation ... the normal, conformant path". Every flags field is
    # therefore accepted in silence, and the bit survives uninterpreted.
    accept(zpf.FileHeader(tick_hz=1, flags=zpf.FileFlags(0x0002)))
    accept(HEADER, zpf.Session(session_id=5, flags=zpf.SessionFlags(0x0002)))
    accept(*RAW_PRELUDE, raw_record(flags=zpf.RecordFlags(0x2000)))
    accept(*RAW_PRELUDE, raw_record(flags=zpf.RecordFlags(0x2000) | zpf.RecordFlags.PSH))


def test_a_block_with_reserved_bits_is_still_absorbed():
    # The cascade this prevents: a dropped File Header would make every later
    # block "first block must be a File Header", emptying the whole file, and
    # a dropped Session Descriptor would take its participants and records.
    checker = accept(zpf.FileHeader(tick_hz=1, flags=zpf.FileFlags(0x0002)))
    checker.check([CAP, SESS, PART, raw_record()])  # the header counted; the file reads on
    checker.finish()

    checker = accept(HEADER, CAP, zpf.Session(session_id=5, flags=zpf.SessionFlags(0x0002)))
    checker.check([PART, raw_record()])  # the session counted; its records belong to it


def test_prim_width_binds_payload_len():
    accept(*RAW_PRELUDE, raw_record(payload=b"abcd", content_type="prim:u32"))
    accept(*RAW_PRELUDE, raw_record(payload=b"anything", content_type="prim:bytes"))
    accept(*RAW_PRELUDE, raw_record(content_type="mime:text/plain"))  # width binds prim: only
    # A writer MUST NOT emit these, so the finding stands — but it is advisory:
    # the reader is told to ignore the label, not to drop the bytes.
    advise(
        *RAW_PRELUDE, raw_record(payload=b"abc", content_type="prim:u32"),
        match="requires payload_len 4",
    )
    advise(
        *RAW_PRELUDE, raw_record(payload=b"abcd", content_type="prim:u128"),
        match="not a legal prim: token",
    )


def test_an_advisory_record_is_absorbed_before_it_is_reported():
    # A lenient reader keeps such a record, so the checker must have counted
    # it: the seq cursor advanced and the file kind locked.
    checker = advise(
        *RAW_PRELUDE,
        raw_record(payload=b"abc", content_type="prim:u32", seq_start=100),
        match="payload_len",
    )
    # The seq cursor advanced, which is what proves the record was counted.
    checker.observe(raw_record(seq_start=200))
    with pytest.raises(zpf.SemanticError, match="seq_start order"):
        checker.observe(raw_record(seq_start=50))


def test_an_isolating_violation_outranks_an_advisory_one():
    # Same record, two findings: the one that costs the caller the block wins,
    # since the checker must not absorb a record it is about to isolate — and
    # the advisory notes go with the dropped block rather than being reported.
    reject(
        *RAW_PRELUDE, raw_record(payload=b"abc", content_type="prim:u32", decoder_id=9),
        match="undeclared decoder",
    )
    reject(
        *RAW_PRELUDE,
        raw_record(seq_start=100),
        raw_record(payload=b"abc", content_type="prim:u32", flags=zpf.RecordFlags(0x2000),
                   seq_start=50),
        match="seq_start order",
    )


# --- Positive cases: real files pass -----------------------------------------------------


def test_the_golden_file_passes():
    finished(*GOLDEN_BLOCKS)


@pytest.mark.parametrize(
    "example", [MERGED_EXAMPLE, DECODED_EXAMPLE], ids=["merged", "decoded"]
)
def test_the_spec_derived_examples_pass(example: str):
    finished(*zpf.JsonlReader(io.StringIO(example)))


# --- Flat-writer wiring -------------------------------------------------------------------


def test_flat_writers_check_only_when_asked():
    violating = [HEADER, raw_record()]  # record with nothing declared
    with zpf.BlockWriter(io.BytesIO()) as unchecked:
        for block in violating:
            unchecked.write(block)  # default: permissive
    with zpf.BlockWriter(io.BytesIO(), check=True) as checked:
        checked.write(HEADER)
        with pytest.raises(zpf.SemanticError):
            checked.write(raw_record())
    with zpf.JsonlWriter(io.StringIO(), check=True) as jsonl_checked:
        jsonl_checked.write(HEADER)
        with pytest.raises(zpf.SemanticError):
            jsonl_checked.write(raw_record())


def test_writers_still_refuse_an_advisory_violation():
    # The writer obligation is unchanged by the reader's leniency: a checking
    # writer refuses a prim: label its payload cannot hold. A reserved flag
    # bit is *not* refused — it is conformant extension surface, not a
    # violation the writer must be protected from.
    with zpf.BlockWriter(io.BytesIO(), check=True) as checked:
        for block in RAW_PRELUDE:
            checked.write(block)
        with pytest.raises(zpf.AdvisoryError, match="requires payload_len 4"):
            checked.write(raw_record(payload=b"abc", content_type="prim:u32"))
        checked.write(raw_record(flags=zpf.RecordFlags(0x2000)))
    sink = io.BytesIO()
    with zpf.create(sink, tick_hz=1) as writer:  # the ergonomic writer always checks
        writer.add_source("capture")
        session = writer.begin_session(session_id=5)
        sender = session.participant("a")
        with pytest.raises(zpf.AdvisoryError, match="not a legal prim: token"):
            session.record(sender, ts=0, payload=b"abcd", content_type="prim:u128")


# --- Blocks a raw file may not carry (0.13/0.14) ------------------------------


def test_a_raw_file_may_not_carry_a_discontinuity():
    """The block marks a break in a decoded stream this file produced.

    A raw capture's offsets are true stream positions, in which a hole is
    already the space between two ``seq_start``s — there is nothing for the
    block to add and no decoded space for it to speak about.
    """
    reject(
        HEADER, CAP, SESS, PART,
        raw_record(),
        zpf.Discontinuity(session_id=5, participant_id=0, width=25),
        match="Discontinuity",
    )


def test_a_raw_file_may_not_declare_input_extents():
    """They measure an input stream, which a raw capture has not got."""
    reject(
        HEADER, CAP, SESS, PART,
        raw_record(),
        zpf.SessionEnd(
            session_id=5,
            input_extents=(
                zpf.InputExtent(source_id=2, session_id=7, participant_id=0, extent=100),
            ),
        ),
        match="input_extents",
    )


def test_a_raw_file_may_not_carry_a_transform_params_digest():
    """It names the configuration of a transform that produced the records."""
    reject(
        zpf.FileHeader(tick_hz=1, transform_params_digest="sha256:abcd"),
        CAP, SESS, PART,
        raw_record(),
        match="transform_params_digest",
    )


def test_a_discontinuity_is_fine_in_a_decode_stage():
    finished(
        DERIVED_HEADER, INP, DEC, SESS, PART,
        zpf.Record(
            session_id=5, sender_pid=0, source_id=2, timestamp=0, payload=b"x",
            decoder_id=3, spans=(Span(source_id=2, session_id=7, participant_id=0,
                                      off_start=0, off_end=1),),
        ),
        zpf.Discontinuity(session_id=5, participant_id=0, width=25),
    )


def test_a_discontinuity_names_a_participant_of_its_own_file():
    reject(
        DERIVED_HEADER, INP, DEC, SESS, PART,
        zpf.Discontinuity(session_id=5, participant_id=9),
        match="undeclared participant",
    )



def reject_at_end(*blocks: zpf.Block, match: str) -> None:
    """Assert the stream's first *end-of-stream* finding matches.

    The coverage guarantee is settled only by ``finish``: nothing about a
    hole is knowable until every block has been seen, so ``reject`` — which
    stops at ``check`` — would find nothing here.
    """
    with pytest.raises(zpf.SemanticError, match=match):
        finished(*blocks)


# --- Coverage and extents, from the file alone --------------------------------


def _decode_stage(*tail: zpf.Block) -> tuple[zpf.Block, ...]:
    """Return a minimal decode stage, plus whatever the case appends."""
    return (DERIVED_HEADER, INP, DEC, SESS, PART, *tail)


def _cite(off_start: int, off_end: int, *, pid: int = 0) -> zpf.Record:
    return zpf.Record(
        session_id=5, sender_pid=0, source_id=2, timestamp=0, payload=b"x", decoder_id=3,
        spans=(Span(source_id=2, session_id=7, participant_id=pid,
                    off_start=off_start, off_end=off_end),),
    )


def test_an_interior_hole_is_caught_without_any_declared_extent():
    """A hole *between* covered ranges needs no input file and no extent.

    This is what distinguishes it from a trailing gap, which is invisible
    until something declares how long the stream was.
    """
    reject_at_end(
        *_decode_stage(
            _cite(0, 10),
            zpf.Undecoded(source_id=2, session_id=7, participant_id=0,
                          off_start=20, off_end=50, reason="undecodable"),
        ),
        match=r"\[10, 20\) is neither decoded nor marked",
    )


def test_a_trailing_gap_is_invisible_until_an_extent_declares_it():
    # Same shape, minus the interior hole: coverage simply stops, which is
    # indistinguishable from a stream that was that short.
    finished(*_decode_stage(_cite(0, 20), zpf.SessionEnd(session_id=5)))
    reject_at_end(
        *_decode_stage(
            _cite(0, 20),
            zpf.SessionEnd(
                session_id=5,
                input_extents=(
                    zpf.InputExtent(source_id=2, session_id=7, participant_id=0, extent=40),
                ),
            ),
        ),
        match="declared extent 40",
    )


def test_a_declared_extent_its_own_spans_overshoot_is_a_contradiction():
    reject_at_end(
        *_decode_stage(
            _cite(0, 100),
            zpf.SessionEnd(
                session_id=5,
                input_extents=(
                    zpf.InputExtent(source_id=2, session_id=7, participant_id=0, extent=60),
                ),
            ),
        ),
        match="declared extent 60",
    )


def test_span_on_span_overlap_is_legal():
    """Coverage is *at least* once, so two records may cite the same bytes.

    The case that makes it necessary: one input record's framing can feed an
    inner unit in each of two output sessions. Only span-against-Undecoded is
    a contradiction — that says the same bytes were both decoded and not.
    """
    checker = accept(
        *_decode_stage(
            _cite(0, 80),
            _cite(0, 80),
            zpf.SessionEnd(
                session_id=5,
                input_extents=(
                    zpf.InputExtent(source_id=2, session_id=7, participant_id=0, extent=80),
                ),
            ),
        )
    )
    assert checker.coverage_findings() == []


def test_a_pass_through_is_not_held_to_the_coverage_guarantee():
    """It re-emits records rather than citing them, so it has no spans.

    Holding it to a decode stage's obligation would read the space before its
    inherited Undecoded blocks as an unaccounted hole — which is how three
    conformant vectors would fail.
    """
    origin = Origin(source_id=2, session_id=7, participant_id=0)
    checker = finished(
        DERIVED_HEADER, INP, DEC, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=origin),
        zpf.Record(session_id=5, sender_pid=0, source_id=2, timestamp=0,
                   payload=b"x", decoder_id=3),
        zpf.Undecoded(source_id=2, session_id=7, participant_id=0,
                      off_start=100, off_end=139, reason="undecodable"),
    )
    assert checker.coverage_findings() == []


# --- Per-participant classification (0.16) ---------------------------------------------


def test_one_file_may_decode_one_stream_and_pass_another_through():
    """``mixed-derivation``'s shape, and what file purity could not express.

    The old rule left a tool with a decoder for one protocol and not the
    other two dishonest options: pass everything through, or mark the second
    stream entirely Undecoded, which drops those bytes from the output.
    """
    span = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1)
    finished(
        DERIVED_HEADER, INP, DEC, SESS,
        zpf.Participant(session_id=5, participant_id=0),  # created
        zpf.Participant(session_id=5, participant_id=1, origin=ORIGIN),  # preserved
        raw_record(source_id=2, sender_pid=0, decoder_id=3, spans=(span,)),
        raw_record(source_id=2, sender_pid=1, decoder_id=3),
    )


def test_a_stream_resolving_to_an_undefined_layer_is_isolated():
    """Absence and an unrecognised value are different statements.

    Falling back to the absent-means-decoded default would read a transport
    stream's offsets as a payload concatenation, silently.
    """
    reject(
        DERIVED_HEADER, INP, zpf.Decoder(decoder_id=3, output_layer=9), SESS, PART,
        raw_record(source_id=2, decoder_id=3, spans=(
            Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1),
        )),
        match="does not define",
    )


def test_two_decoders_in_one_session_are_ordinary():
    """What is NOT wrong: the rule is per participant, not per session."""
    other = zpf.Decoder(decoder_id=4, name="tls")
    span = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1)
    finished(
        DERIVED_HEADER, INP, DEC, other, SESS,
        PART, zpf.Participant(session_id=5, participant_id=1),
        raw_record(source_id=2, sender_pid=0, decoder_id=3, spans=(span,)),
        raw_record(source_id=2, sender_pid=1, decoder_id=4, spans=(span,)),
    )


def test_a_transport_stream_may_not_carry_a_discontinuity():
    """Barred by the layer, whatever the provenance.

    Its offsets are already hole-inclusive, so a gap occupies a real range
    no payload covers; a second account of the same missing bytes would have
    no rule for which to believe.
    """
    reject(
        HEADER, CAP, SESS, PART,
        raw_record(),  # no decoder: transport
        zpf.Discontinuity(session_id=5, participant_id=0, width=25),
        match="MUST NOT carry a Discontinuity",
    )


def test_a_capture_only_file_may_not_carry_a_transform_params_digest():
    """Stated as placement, not as the absence of a transform.

    Reassembly *is* a transform, and a destructive one — but a reassembler
    wanting its configuration recorded declares itself as a Decoder and puts
    it in that descriptor's ``params_digest``. This option is for a stage
    that produced records **without** decoding, which such a file has none of.
    """
    header = zpf.FileHeader(tick_hz=1, transform_params_digest="sha256:ab")
    reject(header, CAP, SESS, PART, raw_record(), match="every stream in this file is")
    # With one zpf-sourced stream in the file, the option has a stage to
    # belong to and the rule does not bind.
    finished(
        zpf.FileHeader(tick_hz=1, produced_by="t", produced_at=1,
                       transform_params_digest="sha256:ab"),
        INP, SESS, zpf.Participant(session_id=5, participant_id=0, origin=ORIGIN),
        raw_record(source_id=2),
    )

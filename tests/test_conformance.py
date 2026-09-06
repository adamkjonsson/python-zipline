"""Tests for the semantic conformance checker and its flat-writer wiring."""

from __future__ import annotations

import io

import pytest
from test_golden import GOLDEN_BLOCKS
from test_jsonl import DECODED_EXAMPLE, MERGED_EXAMPLE

import zpf
from zpf.blocks import Span

HEADER = zpf.FileHeader(tick_hz=1)
DERIVED_HEADER = zpf.FileHeader(tick_hz=1, produced_by="tool 1.0", produced_at=1_719_500_000)
CAP = zpf.Source(source_id=1, kind=zpf.SourceKind.CAPTURE)
INP = zpf.Source(source_id=2, kind=zpf.SourceKind.ZPF_INPUT)
DEC = zpf.Decoder(decoder_id=3, name="http/1.1")
SESS = zpf.Session(session_id=5)
PART = zpf.Participant(session_id=5, participant_id=0)
IDENTITY = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1)
"""An identity span: the range a pass-through re-emitted unchanged."""


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

#: A capture-sourced *decoded* stream — ``proxy-decoded``'s shape, which needs
#: no derived header. Anything asserting about ``content_type`` belongs here
#: rather than in :data:`RAW_PRELUDE`: since 0.16 a transport-layer record MUST
#: NOT carry the label at all, so a decoder-less fixture would be testing the
#: prim: rules through a violation of a different one.
DECODED_PRELUDE = (HEADER, CAP, DEC, SESS, PART)


def decoded_record(**kwargs: object) -> zpf.Record:
    """Build a record at the decoded layer, for the ``content_type`` rules."""
    kwargs.setdefault("decoder_id", 3)
    return raw_record(**kwargs)


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


def test_a_hintless_sequenced_session_owes_nothing_since_0_19():
    """Package B: SEQUENCED is a bare assertion, taken on trust.

    Through `0.18` a sequenced session whose records carried no ``seq``/``ack``
    MUST have named what its order rested on, and the rule could only be ruled
    on at Session End — hint-lessness being a property of the *records*, which
    declare-on-first-use puts after the descriptor. `0.19` removed the option
    and the rule with it, so the producer is trusted for the basis exactly as
    it already was for the order itself, which a reader cannot check either.

    Both moments are asserted, because both used to raise.
    """
    checker = accept(*hintless_session())
    checker.finish()  # was: SemanticError naming sequenced_basis

    checker = accept(*hintless_session())
    checker.observe(zpf.SessionEnd(session_id=5))  # was: the same, one block earlier


def test_a_hint_anywhere_is_still_not_a_conformance_question():
    # Kept from the basis rule's tests because the shapes are worth holding:
    # a session carrying seq or ack, and one carrying neither, are all
    # accepted, and nothing about SEQUENCED changes that.
    finished(HEADER, CAP, zpf.Session(session_id=5, flags=zpf.SessionFlags.SEQUENCED),
             PART, raw_record(seq_start=1000))
    finished(HEADER, CAP, zpf.Session(session_id=5, flags=zpf.SessionFlags.SEQUENCED),
             PART, raw_record(ack=1000))


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


def test_every_zpf_sourced_record_carries_spans():
    """Package A: one per-record rule where `0.18` had four per-participant.

    The four it replaces all keyed on the ``origin`` option `0.19` removed:
    every pass-through participant carries exactly one; it MUST NOT appear on
    a capture-sourced stream; a participant MUST NOT carry both origin and
    records with spans; a ``zpf``-sourced participant MUST be one or the
    other. What is left is that a record naming a ``zpf-input`` Source says
    which stream inside it the bytes came from, and ``spans`` is how.

    It also fires **earlier**. Those were properties of a participant's whole
    record set, so the checker could only rule at Session End; this is
    decidable at the record, which is the block a lenient reader isolates.
    """
    reject(
        DERIVED_HEADER, INP, SESS, PART,
        raw_record(source_id=2),
        match="carries no spans",
    )
    # It binds on zpf-sourced records alone: a capture-sourced record
    # correctly carries none, its source_id being the whole of its provenance.
    finished(HEADER, CAP, SESS, PART, raw_record())


def test_a_pass_through_states_its_provenance_with_an_identity_span():
    """The shape that replaced ``origin``, and the rule reads it the same way.

    A preserved record's span names the range it re-emitted unchanged — the
    same range in as out. Nothing here distinguishes it from a decode stage's
    span, and that is the point: one rule covers both, and which kind of stage
    wrote a stream is read from whether the spans are identity rather than
    from which option is present.
    """
    finished(
        DERIVED_HEADER, INP, SESS, PART,
        raw_record(source_id=2, spans=(IDENTITY,)),
    )


def test_decoder_id_decides_neither_axis():
    # A pass-through preserving a decoded layer: records keep decoder_id and
    # content_type, provenance is an identity span, and inherited Undecoded
    # blocks ride along. 0.9 could not express this, and a strict 0.9 reader
    # refuses it.
    finished(
        DERIVED_HEADER, INP, DEC, SESS, PART,
        raw_record(source_id=2, decoder_id=3, content_type="dec:request",
                   spans=(IDENTITY,)),
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


# --- Derived header, reserved bits, prim widths ----------------------------------------


def test_derived_files_must_declare_their_provenance():
    # The record carries spans, so it clears the provenance rule and reaches
    # the one this test is about. Without them the spans rule fires first and
    # the assertion passes for the wrong reason.
    reject(
        HEADER, INP, DEC, SESS, PART,
        raw_record(source_id=2, decoder_id=3, spans=(IDENTITY,)),
        match="produced_by and produced_at",
    )


def test_reserved_flag_bits_are_not_a_violation():
    # The specification groups a nonzero reserved field with unknown block
    # types and unknown option ids: part of the extension mechanism, "not a
    # violation ... the normal, conformant path". Every flags field is
    # therefore accepted in silence, and the bit survives uninterpreted.
    # The File Header lost its flags field with SINGLE_CLOCK in 0.19, so a
    # reserved bit there is now an unknown *option* instead — the other arm
    # of the same extension mechanism, covered by the escape tests.
    accept(HEADER, zpf.Session(session_id=5, flags=zpf.SessionFlags(0x0002)))
    accept(*RAW_PRELUDE, raw_record(flags=zpf.RecordFlags(0x2000)))
    accept(*RAW_PRELUDE, raw_record(flags=zpf.RecordFlags(0x2000) | zpf.RecordFlags.PSH))


def test_a_block_with_reserved_bits_is_still_absorbed():
    # The cascade this prevents: a dropped File Header would make every later
    # block "first block must be a File Header", emptying the whole file, and
    # a dropped Session Descriptor would take its participants and records.
    checker = accept(HEADER, CAP, zpf.Session(session_id=5, flags=zpf.SessionFlags(0x0002)))
    checker.check([PART, raw_record()])  # the session counted; its records belong to it


def test_prim_width_binds_payload_len():
    accept(*DECODED_PRELUDE, decoded_record(payload=b"abcd", content_type="prim:u32"))
    accept(*DECODED_PRELUDE, decoded_record(payload=b"anything", content_type="prim:bytes"))
    # width binds prim: only
    accept(*DECODED_PRELUDE, decoded_record(content_type="mime:text/plain"))
    # A writer MUST NOT emit these, so the finding stands — but it is advisory:
    # the reader is told to ignore the label, not to drop the bytes.
    advise(
        *DECODED_PRELUDE, decoded_record(payload=b"abc", content_type="prim:u32"),
        match="requires payload_len 4",
    )
    advise(
        *DECODED_PRELUDE, decoded_record(payload=b"abcd", content_type="prim:u128"),
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
            session.record(
                sender,
                ts=0,
                payload=b"abcd",
                decoded=zpf.Decoded(content_type="prim:u128"),
            )


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
    """A file answers for an input stream **some record of it cited**.

    Through `0.18` this test held a pass-through, which cited nothing, and the
    gate existed to keep a decode stage's obligation off it. Since `0.19` a
    pass-through cites everything, so the shape that still needs the gate is
    the other one the specification names: a ``zpf-input`` Source declared
    only so that an *inherited* Undecoded block still resolves. No record's
    spans name it, so this file is not answerable for it, and the region
    below the block is not an unaccounted hole.
    """
    checker = finished(
        DERIVED_HEADER, INP, DEC, SESS, PART,
        zpf.Record(session_id=5, sender_pid=0, source_id=2, timestamp=0,
                   payload=b"x", decoder_id=3,
                   spans=(Span(source_id=2, session_id=9, participant_id=0,
                               off_start=0, off_end=1),)),
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
    created = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=40)
    finished(
        DERIVED_HEADER, INP, DEC, SESS,
        zpf.Participant(session_id=5, participant_id=0),  # created
        zpf.Participant(session_id=5, participant_id=1),  # preserved
        raw_record(source_id=2, sender_pid=0, decoder_id=3, spans=(created,)),
        raw_record(source_id=2, sender_pid=1, decoder_id=3, spans=(IDENTITY,)),
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
        INP, SESS, PART,
        raw_record(source_id=2, spans=(IDENTITY,)),
    )


# --- The unmarked-break predicate (0.16) -----------------------------------------------

def _range(start: int, end: int, pid: int = 0) -> Span:
    return Span(source_id=2, session_id=9, participant_id=pid, off_start=start, off_end=end)


def _unit(*spans: Span, ts: int = 0) -> zpf.Record:
    return raw_record(source_id=2, decoder_id=3, timestamp=ts, spans=spans)


def _hole(start: int, end: int, pid: int = 0) -> zpf.Undecoded:
    return zpf.Undecoded(
        source_id=2, session_id=9, participant_id=pid,
        off_start=start, off_end=end, reason="gap",
    )


DECODE_PRELUDE = (DERIVED_HEADER, INP, DEC, SESS, PART)


def test_a_hole_between_adjacent_units_requires_a_discontinuity():
    """``isolate-unmarked-break``: the one case decidable from a single file.

    No bytes existed there, so no content can have been carried forward and
    the two units cannot join.
    """
    reject(
        *DECODE_PRELUDE,
        _unit(_range(0, 100)), _hole(100, 139), _unit(_range(139, 200), ts=1),
        match="a Discontinuity between them is required",
    )


def test_the_break_is_satisfied_by_a_discontinuity_between_them():
    """``filtered-decoded``'s shape: the duty discharged, so nothing is owed."""
    finished(
        *DECODE_PRELUDE,
        _unit(_range(0, 100)),
        _hole(100, 139),
        zpf.Discontinuity(session_id=5, participant_id=0, width=39),
        _unit(_range(139, 200), ts=1),
    )


def test_a_bytes_class_region_between_two_units_owes_nothing():
    """``undecoded-skipped``: a discarded BOM, and the survivors do join.

    The reason word is not what decides it — the class is. This is the
    contrast the predicate is deliberately silent about.
    """
    finished(
        *DECODE_PRELUDE,
        _unit(_range(0, 100)),
        zpf.Undecoded(source_id=2, session_id=9, participant_id=0,
                      off_start=100, off_end=139, reason="skipped"),
        _unit(_range(139, 200), ts=1),
    )


def test_the_predicate_does_not_reach_a_transport_stream():
    """``sessionization-stage`` and ``tunnel/inner``, excluded by the layer test.

    Such a stream expresses the same break in its hole-inclusive offsets and
    is *forbidden* the block, so a checker without this clause rejects a
    conformant file.
    """
    reassembler = zpf.Decoder(decoder_id=3, output_layer=zpf.OutputLayer.TRANSPORT)
    finished(
        DERIVED_HEADER, INP, reassembler, SESS, PART,
        _unit(_range(0, 50)), _hole(50, 75), _unit(_range(75, 105), ts=1),
    )


def test_a_stream_only_one_of_the_pair_cites_says_nothing():
    """``session-fan-out``: adjacent units may cite different input streams.

    A hole in a stream only one of them names says nothing about whether
    *they* join.
    """
    finished(
        *DECODE_PRELUDE,
        _unit(_range(0, 100, pid=0)),
        _hole(100, 139, pid=0),
        _unit(_range(0, 100, pid=1), ts=1),
    )


def test_input_regions_running_backwards_are_not_tested():
    """``reordered-decoded``: where A >= B, "the region between them" names nothing."""
    finished(
        *DECODE_PRELUDE,
        _unit(_range(139, 200)),
        _hole(100, 139),
        _unit(_range(0, 100), ts=1),
    )


def test_overlapping_spans_are_reduced_by_max_and_min():
    """Legal since 0.14, and the reduction is what keeps them from firing."""
    finished(
        *DECODE_PRELUDE,
        _unit(_range(0, 100), _range(80, 150)),  # A = max(100, 150) = 150
        _hole(100, 139),
        _unit(_range(120, 200), _range(150, 260), ts=1),  # B = min(120, 150) = 120
    )


# --- Placement: the report and the handshake shape (#63) -------------------------------


def anchored(*records: zpf.Block, isn: int | None = 1000) -> zpf.ConformanceChecker:
    """Feed a capture session whose participant declares ``isn``."""
    checker = zpf.ConformanceChecker()
    checker.check([
        HEADER, CAP,
        zpf.Session(session_id=5),
        zpf.Participant(session_id=5, participant_id=0, isn=isn),
    ])
    for record in records:
        checker.observe(record)
    return checker


def notes(*records: zpf.Block, isn: int | None = 1000) -> list[str]:
    """Return the placement notes the last of ``records`` produced."""
    checker = anchored(*records, isn=isn)
    return list(checker.unplaceable_notes)


def test_a_below_origin_record_is_noted_and_not_a_violation():
    """`0.19` withdrew the MUST NOT and kept the effect, so this reports only.

    Raising here — or reporting it as advisory — would make a checking writer
    refuse a block the format permits, and would fail `unplaceable-below-origin`,
    which declares zero violations on the accept tier.
    """
    reported = notes(raw_record(seq_start=1000, payload=b"LOSTBYTE"))
    assert len(reported) == 1
    assert "below the stream origin 1001" in reported[0]
    assert "excluded from the extent" in reported[0]


def test_a_record_at_the_origin_is_placeable():
    assert notes(raw_record(seq_start=1001, payload=b"AAAA")) == []


def test_a_hintless_record_on_an_anchored_stream_is_noted():
    assert len(notes(raw_record(seq_start=None, payload=b"x"))) == 1
    # ...and on a stream with no isn and no earlier hint, it is not: nothing
    # says that stream is sequence-anchored at all.
    assert notes(raw_record(seq_start=None, payload=b"x"), isn=None) == []


def test_an_earlier_hint_anchors_the_stream_for_what_follows():
    checker = anchored(raw_record(seq_start=1001, payload=b"AAAA"), isn=None)
    checker.observe(raw_record(seq_start=None, payload=b"x"))
    assert len(checker.unplaceable_notes) == 1


def test_a_syn_away_from_the_origin_is_advisory():
    """A MUST on the writer whose breach costs a reader the handshake's timing.

    Advisory wherever the record sits, which `0.18` settled: `0.17` had left a
    syn *above* the origin isolatable while the shape that wrecks the offset
    space was accept-and-report, which inverted the strengths against the
    damage.
    """
    for seq_start in (1000, 1007):
        with pytest.raises(zpf.AdvisoryError, match="handshake record MUST sit"):
            anchored(raw_record(
                seq_start=seq_start, payload=b"", flags=zpf.RecordFlags.SYN,
            ))
    # At the origin it is the shape the format describes.
    anchored(raw_record(seq_start=1001, payload=b"", flags=zpf.RecordFlags.SYN))


def test_a_syn_below_the_origin_is_both_advisory_and_unplaceable():
    """#63's actual file: one record, two true things said about it."""
    checker = zpf.ConformanceChecker()
    checker.check([
        HEADER, CAP, zpf.Session(session_id=5),
        zpf.Participant(session_id=5, participant_id=0, isn=1000),
    ])
    with pytest.raises(zpf.AdvisoryError, match="handshake record MUST sit"):
        checker.observe(raw_record(seq_start=1000, payload=b"", flags=zpf.RecordFlags.SYN))
    # The note is recorded even though the advisory finding raised: the block
    # was fully absorbed before `observe` reported it, which is the same
    # ordering that lets a lenient reader keep an advisory block.
    assert len(checker.unplaceable_notes) == 1


# --- role at the transport layer: one check, two labels (#58) --------------------------


def test_either_label_at_the_transport_layer_is_advisory():
    """One rule, one strength, one check — the format states it once.

    A transport record's boundaries are wherever the reassembler chunked the
    stream, so both labels assert a unit where there is a slice. Advisory
    rather than isolating, because dropping a label loses nothing and the
    record stays fully readable.
    """
    for label in ({"content_type": "prim:bytes"}, {"role": "segment"}):
        with pytest.raises(zpf.AdvisoryError, match="MUST NOT carry"):
            accept(*RAW_PRELUDE, raw_record(**label))


def test_both_labels_at_once_are_one_finding():
    """A block with several advisory findings reports them in one message."""
    with pytest.raises(zpf.AdvisoryError) as caught:
        accept(*RAW_PRELUDE, raw_record(content_type="prim:bytes", role="segment"))
    assert "content_type" in str(caught.value)
    assert "role" in str(caught.value)


def test_role_at_the_decoded_layer_is_silent():
    """Where it belongs, it is not a finding at all."""
    finished(
        DERIVED_HEADER, INP, DEC, SESS, PART,
        raw_record(source_id=2, decoder_id=3, payload=b"\x00\x00\x00\x07",
                   content_type="prim:u32", role="checksum", spans=(IDENTITY,)),
    )

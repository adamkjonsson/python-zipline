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
    checker = zpf.ConformanceChecker()
    checker.check(blocks)
    return checker


def reject(*blocks: zpf.Block, match: str) -> None:
    """Assert the sequence's first finding isolates its block."""
    with pytest.raises(zpf.SemanticError, match=match) as caught:
        accept(*blocks)
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


def test_raw_and_derived_records_never_mix():
    decoded = raw_record(
        source_id=2, decoder_id=3,
        spans=(Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1),),
    )
    reject(
        DERIVED_HEADER, CAP, INP, DEC, SESS, PART,
        raw_record(),  # raw byte run from a capture source
        decoded,
        match="exactly one kind",
    )
    reject(
        DERIVED_HEADER, CAP, INP, DEC, SESS, PART,
        decoded,
        raw_record(),
        match="exactly one kind",
    )


def test_decoded_record_must_reference_a_zpf_input_source():
    reject(
        DERIVED_HEADER, CAP, DEC, SESS, PART,
        raw_record(source_id=1, decoder_id=3),
        match="zpf-input",
    )


def test_undecoded_only_in_decode_stage_files():
    undecoded = zpf.Undecoded(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=4)
    reject(DERIVED_HEADER, CAP, INP, SESS, PART, raw_record(), undecoded, match="exactly one kind")
    reject(
        DERIVED_HEADER, CAP, SESS, PART,
        zpf.Undecoded(source_id=1, session_id=9, participant_id=0, off_start=0, off_end=4),
        match="zpf-input",
    )
    accept(DERIVED_HEADER, INP, DEC, undecoded)


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
        match="exactly one kind",
    )


def test_decoder_id_no_longer_decides_the_file_kind():
    # A pass-through preserving a decoded layer: records keep decoder_id and
    # content_type but carry no spans, provenance is the participants'
    # origin, and inherited Undecoded blocks ride along. 0.9 could not
    # express this, and a strict 0.9 reader refuses it.
    checker = accept(
        DERIVED_HEADER, INP, DEC, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=ORIGIN),
        raw_record(source_id=2, decoder_id=3, content_type="dec:request"),
        zpf.Undecoded(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=4),
    )
    assert checker.file_kind == "pass-through"


def test_span_sources_match_the_record_kind():
    input_span = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1)
    capture_span = Span(source_id=1, session_id=0, participant_id=0, off_start=0, off_end=1)
    accept(*RAW_PRELUDE, raw_record(spans=(capture_span,)))  # capture provenance is fine
    reject(HEADER, CAP, INP, SESS, PART, raw_record(spans=(input_span,)), match="CAPTURE source")
    reject(
        DERIVED_HEADER, INP, DEC, CAP, SESS, PART,
        raw_record(source_id=2, decoder_id=3, spans=(capture_span,)),
        match="ZPF_INPUT source",
    )


# --- Pass-through origins ------------------------------------------------------------


def test_origin_in_a_raw_file_is_a_violation():
    with_origin = zpf.Participant(session_id=5, participant_id=1, origin=ORIGIN)
    reject(HEADER, CAP, INP, SESS, PART, raw_record(), with_origin, match="exactly one kind")


def test_pass_through_requires_origin_on_every_participant():
    # Kind locks at the pass-through record; the earlier origin-less
    # participant is reported retroactively.
    reject(
        DERIVED_HEADER, INP, SESS, PART,
        raw_record(source_id=2),
        match="carries no origin",
    )
    # And a later origin-less participant is caught directly.
    reject(
        DERIVED_HEADER, INP, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=ORIGIN),
        zpf.Participant(session_id=5, participant_id=1),
        match="carries no origin",
    )


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
    assert checker.file_kind == "raw"

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
    assert checker.file_kind == "raw"
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
    checker = accept(*GOLDEN_BLOCKS)
    assert checker.file_kind == "raw"


@pytest.mark.parametrize(
    ("example", "kind"),
    [(MERGED_EXAMPLE, "pass-through"), (DECODED_EXAMPLE, "decode-stage")],
    ids=["merged", "decoded"],
)
def test_the_spec_derived_examples_pass(example: str, kind: str):
    blocks = list(zpf.JsonlReader(io.StringIO(example)))
    checker = accept(*blocks)
    assert checker.file_kind == kind


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

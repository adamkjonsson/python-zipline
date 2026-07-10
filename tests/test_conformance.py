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
    with pytest.raises(zpf.SemanticError, match=match):
        accept(*blocks)


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
    reject(
        DERIVED_HEADER, CAP, INP, DEC, SESS, PART,
        raw_record(),  # raw byte run
        raw_record(source_id=2, decoder_id=3),  # decoded
        match="exactly one kind",
    )
    reject(
        DERIVED_HEADER, CAP, INP, DEC, SESS, PART,
        raw_record(source_id=2, decoder_id=3),
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


def test_pass_through_records_carry_no_spans():
    span = Span(source_id=2, session_id=9, participant_id=0, off_start=0, off_end=1)
    reject(
        DERIVED_HEADER, INP, SESS,
        zpf.Participant(session_id=5, participant_id=0, origin=ORIGIN),
        raw_record(source_id=2, spans=(span,)),
        match="must not carry spans",
    )


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


def test_reserved_flag_bits_must_be_zero():
    reject(zpf.FileHeader(tick_hz=1, flags=zpf.FileFlags(0x0002)), match="reserved bits")
    reject(HEADER, zpf.Session(session_id=5, flags=zpf.SessionFlags(0x0002)), match="reserved")
    reject(*RAW_PRELUDE, raw_record(flags=zpf.RecordFlags(0x2000)), match="reserved bits")


def test_prim_width_binds_payload_len():
    accept(*RAW_PRELUDE, raw_record(payload=b"abcd", content_type="prim:u32"))
    reject(
        *RAW_PRELUDE, raw_record(payload=b"abc", content_type="prim:u32"),
        match="requires payload_len 4",
    )
    accept(*RAW_PRELUDE, raw_record(payload=b"anything", content_type="prim:bytes"))
    reject(
        *RAW_PRELUDE, raw_record(payload=b"abcd", content_type="prim:u128"),
        match="not a legal prim: token",
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

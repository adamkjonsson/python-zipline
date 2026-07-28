"""Tests for zpf.content: the content_type grammar and the normative prim: decode."""

from __future__ import annotations

import pytest
from test_conformance import RAW_PRELUDE, raw_record

import zpf
from zpf.content import PRIM_BYTES, PRIM_WIDTHS, ContentType, decode_prim

# --- The label grammar ------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "scheme", "value"),
    [
        ("prim:u32", "prim", "u32"),
        ("prim:bytes", "prim", "bytes"),
        ("mime:text/plain", "mime", "text/plain"),
        # Only the first colon separates: parameters and colons inside the
        # value are the value's business, not the grammar's.
        ("mime:text/plain; charset=utf-8", "mime", "text/plain; charset=utf-8"),
        ("dec:request", "dec", "request"),
        ("dec:ns:inner", "dec", "ns:inner"),
        ("x-private:thing", "x-private", "thing"),  # unknown scheme: opaque, not an error
        ("prim:", "prim", ""),
        (":u32", "", "u32"),  # no scheme named
        ("", "", ""),
    ],
)
def test_parse_splits_on_the_first_colon(label: str, scheme: str, value: str):
    assert ContentType.parse(label) == ContentType(scheme=scheme, value=value)


def test_a_label_without_a_colon_names_no_scheme():
    # It must not accidentally read as a scheme with an empty value, or a
    # bare "prim" would look like the prim: scheme.
    assert ContentType.parse("prim") == ContentType(scheme="", value="prim")
    assert not ContentType.parse("prim").is_prim
    assert not ContentType.parse("text/plain").is_prim


def test_is_prim_is_an_exact_comparison():
    assert ContentType.parse("prim:u8").is_prim
    # No normalizing, no case folding: the vocabulary is closed and lowercase.
    assert not ContentType.parse("PRIM:u8").is_prim
    assert not ContentType.parse("prim2:u8").is_prim
    assert not ContentType.parse("mime:application/prim").is_prim


def test_a_parsed_label_is_a_frozen_value():
    parsed = ContentType.parse("prim:u32")
    assert parsed == ContentType.parse("prim:u32")  # comparable by value
    with pytest.raises(AttributeError):
        parsed.scheme = "mime"  # type: ignore[misc]


# --- The prim: vocabulary ----------------------------------------------------------


def test_the_vocabulary_is_closed_and_the_widths_are_the_token_bits():
    assert set(PRIM_WIDTHS) == {"u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64"}
    for token, width in PRIM_WIDTHS.items():
        assert width * 8 == int(token[1:])
    assert PRIM_BYTES not in PRIM_WIDTHS  # bytes has no width; any length holds


def test_the_vocabulary_cannot_be_extended_at_runtime():
    with pytest.raises(TypeError):
        PRIM_WIDTHS["u128"] = 16  # type: ignore[index]


# --- Decoding integer tokens -------------------------------------------------------


def boundary_values(token: str, width: int) -> list[int]:
    """Return the interesting values of a token's range: 0, ±1, min, max."""
    bits = width * 8
    if token.startswith("i"):
        return [0, 1, -1, -(2 ** (bits - 1)), 2 ** (bits - 1) - 1]
    return [0, 1, 2**bits - 1]


@pytest.mark.parametrize("token", sorted(PRIM_WIDTHS))
def test_every_integer_token_round_trips_its_boundary_values(token: str):
    width = PRIM_WIDTHS[token]
    signed = token.startswith("i")
    for value in boundary_values(token, width):
        payload = value.to_bytes(width, "little", signed=signed)
        assert len(payload) == width
        assert decode_prim(payload, token) == value


def test_integers_are_little_endian():
    # The one property no round-trip through to_bytes() could catch.
    assert decode_prim(b"\x01\x00", "u16") == 1
    assert decode_prim(b"\x00\x01", "u16") == 256
    assert decode_prim(b"\x01\x00\x00\x00", "u32") == 1
    assert decode_prim(b"\xd2\x04\x00\x00", "u32") == 1234
    assert decode_prim(b"\x00\x00\x00\x00\x00\x00\x00\x80", "i64") == -(2**63)


def test_signedness_comes_from_the_tokens_prefix():
    # Same bytes, two labels: the token decides, nothing else.
    assert decode_prim(b"\xff", "u8") == 255
    assert decode_prim(b"\xff", "i8") == -1
    assert decode_prim(b"\xff\xff\xff\xff", "u32") == 4294967295
    assert decode_prim(b"\xff\xff\xff\xff", "i32") == -1


# --- Decoding prim:bytes -----------------------------------------------------------


@pytest.mark.parametrize("payload", [b"", b"\x00", b"anything at all", bytes(range(256))])
def test_prim_bytes_hands_back_the_payload_itself(payload: bytes):
    assert decode_prim(payload, PRIM_BYTES) is payload  # never copied, never checked


# --- The opaque cases: None, never an exception ------------------------------------


@pytest.mark.parametrize(
    ("payload", "token", "why"),
    [
        (b"\x01\x02\x03", "u32", "payload one byte short of the width"),
        (b"\x01\x02\x03\x04\x05", "u32", "payload one byte long"),
        (b"", "u8", "empty payload cannot hold a u8"),
        (b"\x01", "u16", "shorter than the width"),
        (b"\x01" * 8, "u32", "longer than the width"),
        (b"\x01" * 16, "u128", "not in the closed vocabulary"),
        (b"\x01", "U8", "the vocabulary is lowercase"),
        (b"\x01", "u8 ", "no trimming: a token is exact"),
        (b"\x01", "int8", "another spelling is still unknown"),
        (b"\x01", "", "an empty token"),
        (b"\x01", "Bytes", "prim:bytes is lowercase too"),
        (b"\x01", "prim:u8", "the scheme belongs to parse(), not here"),
    ],
)
def test_an_unusable_label_is_opaque_not_an_error(payload: bytes, token: str, why: str):
    # The spec's fallback rule: the reader MUST NOT pad, truncate, or
    # reinterpret — it treats the label as unknown and keeps the bytes.
    assert decode_prim(payload, token) is None, why


# --- The boundary with the conformance checker -------------------------------------


@pytest.mark.parametrize(
    ("payload", "token"),
    [
        (b"\x01", "u8"),
        (b"\x01\x02", "u16"),
        (b"\x01\x02\x03\x04", "i32"),
        (b"anything", PRIM_BYTES),
        (b"\x01", "u32"),
        (b"\x01\x02\x03\x04\x05", "u32"),
        (b"", "u8"),
        (b"\x01" * 16, "u128"),
    ],
)
def test_opacity_and_the_advisory_finding_are_complements(payload: bytes, token: str):
    """The checker flags exactly the prim: labels ``decode_prim`` calls opaque.

    Two modules read the same rule; if they ever disagree, one of them lets a
    record through with a label it cannot honour, or reports a conformant one.
    """
    record = raw_record(payload=payload, content_type=f"prim:{token}")
    checker = zpf.ConformanceChecker()
    try:
        checker.check([*RAW_PRELUDE, record])
    except zpf.AdvisoryError:
        flagged = True
    else:
        flagged = False
    assert flagged == (decode_prim(payload, token) is None)


def test_the_checker_leaves_other_schemes_to_their_handlers():
    # Only prim: is fully spec-defined, so only prim: can be nonconformant
    # here — a mime:/dec:/unknown label is never the checker's business.
    for label in ("mime:text/plain", "dec:request", "x-private:thing", "prim", ""):
        zpf.ConformanceChecker().check([*RAW_PRELUDE, raw_record(content_type=label)])


def test_the_top_level_re_exports_are_the_flat_modules():
    assert zpf.ContentType is ContentType
    assert zpf.decode_prim is decode_prim
    assert zpf.PRIM_WIDTHS is PRIM_WIDTHS

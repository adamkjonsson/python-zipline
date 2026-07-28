"""Tests for zpf.content: the content_type grammar, the prim: decode, the registry."""

from __future__ import annotations

import json

import pytest
from test_conformance import RAW_PRELUDE, raw_record

import zpf
from zpf.content import (
    PRIM_BYTES,
    PRIM_WIDTHS,
    ContentRegistry,
    ContentType,
    decode_prim,
    media_type_of,
    prim_fault,
)

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


# --- Explaining an opaque label -----------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "token", "fault"),
    [
        (b"abc", "u32", "requires payload_len 4, got 3"),
        (b"", "u8", "requires payload_len 1, got 0"),
        (b"\x01" * 9, "u64", "requires payload_len 8, got 9"),
        (b"\x01", "u128", "is not a legal prim: token"),
        (b"\x01", "U8", "is not a legal prim: token"),
    ],
)
def test_a_fault_completes_the_sentence_about_the_label(payload: bytes, token: str, fault: str):
    # The phrasing is shared, so the checker's diagnostic and the strict
    # Record.content() refusal name the same rule in the same words.
    assert prim_fault(payload, token) == fault


@pytest.mark.parametrize(
    ("payload", "token"),
    [(b"\x01", "u8"), (b"\x01\x02\x03\x04", "i32"), (b"", PRIM_BYTES), (b"anything", PRIM_BYTES)],
)
def test_an_honourable_label_has_no_fault(payload: bytes, token: str):
    assert prim_fault(payload, token) is None
    assert decode_prim(payload, token) is not None  # the complement, by construction


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
    block = raw_record(payload=payload, content_type=f"prim:{token}")
    checker = zpf.ConformanceChecker()
    try:
        checker.check([*RAW_PRELUDE, block])
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


# --- Record.content() --------------------------------------------------------------


def record(payload: bytes, content_type: str | None) -> zpf.Record:
    return zpf.Record(
        session_id=0, sender_pid=0, source_id=0, timestamp=0,
        payload=payload, content_type=content_type,
    )


@pytest.mark.parametrize(
    ("payload", "label", "expected"),
    [
        (b"\x2a", "prim:u8", 42),
        (b"\xff", "prim:i8", -1),
        (b"\xd2\x04\x00\x00", "prim:u32", 1234),
        (b"\xff\xff\xff\xff\xff\xff\xff\xff", "prim:i64", -1),
        (b"", "prim:bytes", b""),
        (b"anything", "prim:bytes", b"anything"),
    ],
)
def test_content_interprets_the_spec_defined_scheme(
    payload: bytes, label: str, expected: int | bytes
):
    assert record(payload, label).content() == expected
    # strict changes nothing when the label *can* be honoured.
    assert record(payload, label).content(strict=True) == expected


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        (b"abc", None),  # nothing claimed
        (b"hello", "mime:text/plain"),  # advisory: needs a caller-supplied handler
        (b"hello", "mime:text/plain; charset=utf-8"),
        (b"\x00\x01", "dec:request"),  # namespaced by decoder name — needs the file
        (b"\x00\x01", "x-private:thing"),  # unknown scheme is opaque, not an error
        (b"\x00\x01", "prim"),  # no scheme at all
        (b"abc", "prim:u32"),  # width disagrees: MUST NOT pad
        (b"\x01\x02\x03\x04\x05", "prim:u32"),  # MUST NOT truncate
        (b"", "prim:u8"),
        (b"\x01" * 16, "prim:u128"),  # outside the closed vocabulary
        (b"\x01", "prim:U8"),  # nor case-folded into it
    ],
)
def test_content_falls_back_to_the_payload_untouched(payload: bytes, label: str | None):
    assert record(payload, label).content() == payload


def test_the_fallback_hands_back_the_very_same_bytes():
    # Not a copy, not a reinterpretation: the payload object itself.
    block = record(b"\x01\x02\x03", "prim:u32")
    assert block.content() is block.payload


@pytest.mark.parametrize(
    ("payload", "label", "message"),
    [
        (b"abc", None, "carries no content_type"),
        (b"abc", "prim:u32", "requires payload_len 4, got 3"),
        (b"\x01" * 16, "prim:u128", "is not a legal prim: token"),
        (b"hello", "mime:text/plain", "advisory, not spec-defined"),
        (b"hello", "dec:request", "needs a caller-supplied handler"),
        (b"hello", "x-private:thing", "advisory, not spec-defined"),
    ],
)
def test_strict_refuses_to_pass_off_bytes_as_an_interpretation(
    payload: bytes, label: str | None, message: str
):
    block = record(payload, label)
    assert block.content() == payload  # lenient: the spec's own fallback
    with pytest.raises(zpf.ContentError, match=message):
        block.content(strict=True)


def test_a_content_error_is_both_a_zpf_error_and_a_value_error():
    # The package-wide `except zpf.ZpfError` must stay complete, and callers
    # who read the signature as "raises ValueError" must be right too.
    with pytest.raises(zpf.ZpfError):
        record(b"abc", "prim:u32").content(strict=True)
    with pytest.raises(ValueError, match="payload_len"):
        record(b"abc", "prim:u32").content(strict=True)


def test_content_never_touches_the_record():
    block = record(b"\x01\x00", "prim:u16")
    twin = record(b"\x01\x00", "prim:u16")
    assert block.content() == 1
    assert block == twin  # reading the content is not a mutation
    assert block.to_bytes() == twin.to_bytes()  # the bytes on the wire are untouched


# --- ContentRegistry ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("application/json", "application/json"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("Text/Plain", "text/plain"),  # IANA: type and subtype are case-insensitive
        ("  text/plain  ", "text/plain"),
        ("text/plain;charset=utf-8;fmt=flowed", "text/plain"),
        ("", ""),
    ],
)
def test_a_media_type_is_its_label_without_parameters(value: str, expected: str):
    assert media_type_of(value) == expected


def test_mime_handlers_are_found_by_media_type():
    registry = zpf.ContentRegistry()
    registry.register_mime("application/json", json.loads)
    for label in ("mime:application/json", "mime:application/json; charset=utf-8"):
        handler = registry.handler(ContentType.parse(label))
        assert handler is not None
        assert handler(b'{"a": 1}') == {"a": 1}
    # Case-insensitive on the media type, as IANA defines it.
    assert registry.handler(ContentType.parse("mime:Application/JSON")) is json.loads
    assert registry.handler(ContentType.parse("mime:text/plain")) is None


def test_dec_handlers_are_namespaced_by_the_decoder_name():
    registry = zpf.ContentRegistry()
    registry.register_dec("http/1.1", "request", bytes.splitlines)
    label = ContentType.parse("dec:request")
    assert registry.handler(label, decoder_name="http/1.1") is bytes.splitlines
    # A different decoder's "request" is a different type — that is the whole
    # point of the format namespacing dec: tokens by the decoder's name.
    assert registry.handler(label, decoder_name="smtp") is None
    assert registry.handler(label, decoder_name="HTTP/1.1") is None  # matched exactly
    # And an undecoded record (or a decoder with no declared name) cannot
    # resolve a dec: token at all.
    assert registry.handler(label) is None
    assert registry.handler(ContentType.parse("dec:response"), decoder_name="http/1.1") is None


def test_the_registry_never_answers_for_prim_or_an_unknown_scheme():
    registry = zpf.ContentRegistry()
    registry.register_mime("application/json", json.loads)
    registry.register_dec("http/1.1", "request", bytes.splitlines)
    # prim: is normative and built in, so it is never dispatched here...
    assert registry.handler(ContentType.parse("prim:u32")) is None
    # ...and the format calls every other scheme opaque.
    for label in ("x-private:thing", "prim", "", ":u32"):
        assert registry.handler(ContentType.parse(label)) is None


def test_registering_a_key_twice_replaces_the_handler():
    registry = zpf.ContentRegistry()
    registry.register_mime("application/json", json.loads)
    registry.register_mime("application/json", bytes.strip)
    assert registry.handler(ContentType.parse("mime:application/json")) is bytes.strip


@pytest.mark.parametrize(
    ("media_type", "message"),
    [
        ("application/json; charset=utf-8", "carries parameters"),
        ("", "must not be empty"),
        ("   ", "must not be empty"),
    ],
)
def test_an_unusable_mime_registration_is_refused(media_type: str, message: str):
    with pytest.raises(ValueError, match=message):
        zpf.ContentRegistry().register_mime(media_type, json.loads)


@pytest.mark.parametrize(("name", "token"), [("", "request"), ("http/1.1", ""), ("", "")])
def test_an_unusable_dec_registration_is_refused(name: str, token: str):
    with pytest.raises(ValueError, match="needs both a decoder name and a token"):
        zpf.ContentRegistry().register_dec(name, token, bytes.splitlines)


def test_the_top_level_re_exports_are_the_flat_modules():
    assert zpf.ContentType is ContentType
    assert zpf.decode_prim is decode_prim
    assert zpf.PRIM_WIDTHS is PRIM_WIDTHS
    assert zpf.ContentRegistry is ContentRegistry

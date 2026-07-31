"""The JSON-Lines projection of the Zipline Payload Format.

One JSON object per line, semantically lossless against the binary
container: every field's *value* survives a round-trip, but not the exact
bytes (padding, option order, and ``spans`` chunking are not pinned down by
JSONL — digests are defined over the binary form only).

The same frozen block dataclasses (:mod:`zpf.blocks`) are the model for both
faces. :class:`JsonlReader` and :class:`JsonlWriter` mirror
:class:`~zpf.binary.BlockReader` / :class:`~zpf.binary.BlockWriter`, and
:func:`binary_to_jsonl` / :func:`jsonl_to_binary` convert whole files.

Deviations chosen where the specification is silent, all reported through
the lenient/strict issue machinery:

* An unknown *binary* block type projects as the documented extension line
  ``{"type": "unknown", "block_type": 119, "content": "<base64>"}`` and is
  accepted back — the converter stays total and lossless.
* An unknown JSONL key on a known block, and a Record ``flags`` bit with no
  JSON token, cannot be represented on the other face: by default they are
  dropped with a diagnostic; under ``strict=True`` they raise.
* A Source ``kind`` byte outside capture/zpf-input is written as a plain
  number and accepted back.
* A participant with exactly one ``endpoint`` is written as a plain string,
  matching the specification's examples; a list is written for tunnelled
  (multi-endpoint) participants, and both forms are accepted on read.
"""

from __future__ import annotations

import base64
import json
import os
from typing import IO, TYPE_CHECKING, Any

from zpf._frame import RawOption
from zpf.binary import BlockReader, BlockWriter
from zpf.blocks import (
    Block,
    Custom,
    Decoder,
    End,
    FileFlags,
    FileHeader,
    NameResolution,
    Origin,
    Participant,
    Record,
    RecordFlags,
    Session,
    SessionEnd,
    SessionFlags,
    Source,
    SourceKind,
    Span,
    TcpRole,
    Undecoded,
    UnknownBlock,
    unsupported_version,
)
from zpf.conformance import ConformanceChecker
from zpf.errors import (
    Diagnostic,
    EncodeError,
    SemanticError,
    StructuralError,
    TruncatedError,
    ZpfError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_FORMAT_PREFIX = "zipline-payload/"
_SAFE_INT = 2**53  # beyond this, 64-bit values are written as decimal strings

_TIME_UNIT_TO_HZ = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}
_HZ_TO_TIME_UNIT = {hz: unit for unit, hz in _TIME_UNIT_TO_HZ.items()}

_RECORD_FLAG_TOKENS: tuple[tuple[str, RecordFlags], ...] = (
    ("psh", RecordFlags.PSH),
    ("fin", RecordFlags.FIN),
    ("rst", RecordFlags.RST),
    ("syn", RecordFlags.SYN),
    ("urg", RecordFlags.URG),
    ("retransmit", RecordFlags.RETRANSMIT),
    ("message", RecordFlags.MESSAGE),
)
_TOKEN_TO_RECORD_FLAG = dict(_RECORD_FLAG_TOKENS)

_TCP_ROLE_LABELS = {TcpRole.INITIATOR: "initiator", TcpRole.RESPONDER: "responder"}
_LABEL_TO_TCP_ROLE = {label: role for role, label in _TCP_ROLE_LABELS.items()}

_KIND_LABELS = {SourceKind.CAPTURE: "capture", SourceKind.ZPF_INPUT: "zpf-input"}
_LABEL_TO_KIND = {label: kind for kind, label in _KIND_LABELS.items()}


def _ignore_issue(message: str) -> None:
    del message


# --- Value codecs ------------------------------------------------------------


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(value: Any, name: str) -> bytes:
    if not isinstance(value, str):
        msg = f"{name} must be a base64 string, got {type(value).__name__}"
        raise ValueError(msg)
    return base64.b64decode(value, validate=True)


def _num64(value: int) -> int | str:
    """Project a 64-bit integer: a number, or a decimal string beyond 2**53."""
    return value if -_SAFE_INT <= value <= _SAFE_INT else str(value)


def _dec_int(value: Any, name: str) -> int:
    """Accept a JSON number or a decimal string (the 64-bit escape hatch)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        msg = f"{name} must be an integer or decimal string, got {value!r}"
        raise ValueError(msg)
    try:
        return int(value)
    except ValueError as exc:
        msg = f"{name} is not a valid integer: {value!r}"
        raise ValueError(msg) from exc


def _dec_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        msg = f"{name} must be a string, got {type(value).__name__}"
        raise ValueError(msg)
    return value


def _dec_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        msg = f"{name} must be a boolean, got {value!r}"
        raise ValueError(msg)
    return value


# --- format / time_units -----------------------------------------------------


def _format_string(header: FileHeader) -> str:
    if header.version_minor:
        return f"{_FORMAT_PREFIX}{header.version_major}.{header.version_minor}"
    return f"{_FORMAT_PREFIX}{header.version_major}"


def _parse_format(value: Any) -> tuple[int, int]:
    text = _dec_str(value, "format")
    if not text.startswith(_FORMAT_PREFIX):
        msg = f"format {text!r} does not start with {_FORMAT_PREFIX!r}"
        raise ValueError(msg)
    version = text[len(_FORMAT_PREFIX) :]
    major_text, _, minor_text = version.partition(".")
    try:
        return int(major_text), int(minor_text) if minor_text else 0
    except ValueError as exc:
        msg = f"format {text!r} has an unparseable version"
        raise ValueError(msg) from exc


def _time_units(tick_hz: int) -> int | str:
    return _HZ_TO_TIME_UNIT.get(tick_hz) or _num64(tick_hz)


def _parse_time_units(value: Any) -> int:
    if isinstance(value, str) and value in _TIME_UNIT_TO_HZ:
        return _TIME_UNIT_TO_HZ[value]
    return _dec_int(value, "time_units")


# --- Option-list ("options") codec -------------------------------------------


def _options_to_json(extras: tuple[RawOption, ...]) -> list[dict[str, str]]:
    return [{"id": f"0x{opt.option_id:04X}", "value": _b64e(opt.value)} for opt in extras]


def _options_from_json(value: Any) -> tuple[RawOption, ...]:
    if not isinstance(value, list):
        msg = "options must be an array"
        raise ValueError(msg)
    extras = []
    for entry in value:
        if not isinstance(entry, dict) or entry.keys() - {"id", "value"}:
            msg = f"options entry {entry!r} is not {{id, value}}"
            raise ValueError(msg)
        raw_id = entry.get("id")
        option_id = int(raw_id, 16) if isinstance(raw_id, str) else _dec_int(raw_id, "options.id")
        extras.append(RawOption(option_id, _b64d(entry.get("value"), "options.value")))
    return tuple(extras)


# --- Decode-side helper: consume an object's keys, flag the leftovers ---------


class _ObjReader:
    """Consume a JSON block object key by key; leftovers are unknown keys."""

    def __init__(self, obj: Mapping[str, Any]) -> None:
        self._obj = dict(obj)
        self._obj.pop("type", None)

    def take(self, key: str) -> Any:
        """Remove and return a key's value, or ``None`` when absent."""
        return self._obj.pop(key, None)

    def require(self, key: str) -> Any:
        """Remove and return a mandatory (body-field) key's value."""
        if key not in self._obj:
            msg = f"missing required key {key!r}"
            raise ValueError(msg)
        return self._obj.pop(key)

    def take_int(self, key: str) -> int | None:
        value = self.take(key)
        return None if value is None else _dec_int(value, key)

    def require_int(self, key: str) -> int:
        return _dec_int(self.require(key), key)

    def take_str(self, key: str) -> str | None:
        value = self.take(key)
        return None if value is None else _dec_str(value, key)

    def options(self) -> tuple[RawOption, ...]:
        """Remove and decode the generic ``options`` array, if present."""
        value = self.take("options")
        return () if value is None else _options_from_json(value)

    def finish(self, on_issue: Callable[[str], None]) -> None:
        """Report every key nothing claimed; they have no binary encoding."""
        for key in self._obj:
            on_issue(f"unknown key {key!r} has no binary encoding and was dropped")


# --- spans / origin ----------------------------------------------------------


def _span_to_json(span: Span) -> dict[str, Any]:
    return {
        "source_id": span.source_id,
        "session_id": _num64(span.session_id),
        "pid": span.participant_id,
        "off_start": _num64(span.off_start),
        "off_end": _num64(span.off_end),
    }


def _span_from_json(value: Any) -> Span:
    if not isinstance(value, dict):
        msg = f"span entry must be an object, got {value!r}"
        raise ValueError(msg)
    reader = _ObjReader(value)
    span = Span(
        source_id=reader.require_int("source_id"),
        session_id=reader.require_int("session_id"),
        participant_id=reader.require_int("pid"),
        off_start=reader.require_int("off_start"),
        off_end=reader.require_int("off_end"),
    )
    reader.finish(_raise_issue)
    return span


def _origin_to_json(origin: Origin) -> dict[str, Any]:
    return {
        "source_id": origin.source_id,
        "session_id": _num64(origin.session_id),
        "pid": origin.participant_id,
    }


def _origin_from_json(value: Any) -> Origin:
    if not isinstance(value, dict):
        msg = f"origin must be an object, got {value!r}"
        raise ValueError(msg)
    reader = _ObjReader(value)
    origin = Origin(
        source_id=reader.require_int("source_id"),
        session_id=reader.require_int("session_id"),
        participant_id=reader.require_int("pid"),
    )
    reader.finish(_raise_issue)
    return origin


def _raise_issue(message: str) -> None:
    raise ValueError(message)


# --- Per-block encoders (block -> JSON object) --------------------------------


def _put(obj: dict[str, Any], key: str, value: Any) -> None:
    """Set a key unless the value is an absent option (``None``)."""
    if value is not None:
        obj[key] = value


def _enc_file(block: FileHeader, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {"type": "file", "format": _format_string(block)}
    obj["time_units"] = _time_units(block.tick_hz)
    _put(obj, "time_epoch", None if block.time_epoch is None else _num64(block.time_epoch))
    _put(obj, "creator", block.creator)
    _put(obj, "produced_by", block.produced_by)
    _put(obj, "produced_at", None if block.produced_at is None else _num64(block.produced_at))
    if block.flags & FileFlags.SINGLE_CLOCK:
        obj["single_clock"] = True
    _put(obj, "comment", block.comment)
    return obj


def _enc_source(block: Source, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    kind = _KIND_LABELS.get(block.kind, int(block.kind))
    obj: dict[str, Any] = {"type": "source", "source_id": block.source_id, "kind": kind}
    _put(obj, "uri", block.uri)
    _put(obj, "digest", block.digest)
    _put(obj, "link_type", block.link_type)
    _put(obj, "comment", block.comment)
    return obj


def _enc_decoder(block: Decoder, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {"type": "decoder", "decoder_id": block.decoder_id}
    _put(obj, "name", block.name)
    _put(obj, "version", block.version)
    _put(obj, "params_digest", block.params_digest)
    _put(obj, "comment", block.comment)
    return obj


def _enc_session(block: Session, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {"type": "session", "session_id": _num64(block.session_id)}
    _put(obj, "proto", block.proto)
    _put(obj, "key", block.flow_key)
    if block.flags & SessionFlags.SEQUENCED:
        obj["sequenced"] = True
    _put(obj, "comment", block.comment)
    return obj


def _enc_participant(block: Participant, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {
        "type": "participant",
        "session_id": _num64(block.session_id),
        "pid": block.participant_id,
    }
    if len(block.endpoints) == 1:
        obj["endpoint"] = block.endpoints[0]  # scalar form, as in the spec's examples
    elif block.endpoints:
        obj["endpoint"] = list(block.endpoints)
    _put(obj, "isn", block.isn)
    _put(obj, "identity", block.identity)
    if block.tcp_role in _TCP_ROLE_LABELS:  # UNKNOWN and absent both omit, per spec
        obj["tcp_role"] = _TCP_ROLE_LABELS[block.tcp_role]
    if block.origin is not None:
        obj["origin"] = _origin_to_json(block.origin)
    _put(obj, "comment", block.comment)
    return obj


def _enc_session_end(block: SessionEnd, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {"type": "session_end", "session_id": _num64(block.session_id)}
    _put(obj, "reason", block.reason)
    _put(obj, "comment", block.comment)
    return obj


def _record_flag_tokens(flags: RecordFlags, on_issue: Callable[[str], None]) -> list[str]:
    tokens = [token for token, flag in _RECORD_FLAG_TOKENS if flags & flag]
    known = RecordFlags(0)
    for _token, flag in _RECORD_FLAG_TOKENS:
        known |= flag
    unknown = int(flags) & ~int(known)
    if unknown:
        on_issue(f"record flags bits 0x{unknown:04X} have no JSON token and were dropped")
    return tokens


def _enc_record(block: Record, on_issue: Callable[[str], None]) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "type": "record",
        "session_id": _num64(block.session_id),
        "sender_pid": block.sender_pid,
        "source_id": block.source_id,
        "ts": _num64(block.timestamp),
    }
    _put(obj, "seq_start", block.seq_start)
    _put(obj, "ack", block.ack)
    _put(obj, "ts_first", None if block.ts_first is None else _num64(block.ts_first))
    _put(obj, "decoder_id", block.decoder_id)
    if block.spans:
        obj["spans"] = [_span_to_json(span) for span in block.spans]
    _put(obj, "content_type", block.content_type)
    if block.flags:
        obj["flags"] = _record_flag_tokens(block.flags, on_issue)
    obj["payload"] = _b64e(block.payload)
    _put(obj, "comment", block.comment)
    return obj


def _enc_undecoded(block: Undecoded, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {
        "type": "undecoded",
        "source_id": block.source_id,
        "session_id": _num64(block.session_id),
        "pid": block.participant_id,
        "off_start": _num64(block.off_start),
        "off_end": _num64(block.off_end),
    }
    _put(obj, "reason", block.reason)
    _put(obj, "decoder_id", block.decoder_id)
    _put(obj, "comment", block.comment)
    return obj


def _enc_name(block: NameResolution, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {
        "type": "name",
        "session_id": _num64(block.session_id),
        "pid": block.participant_id,
    }
    _put(obj, "label", block.label)
    _put(obj, "kind", block.kind)
    _put(obj, "comment", block.comment)
    return obj


def _enc_end(block: End, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    obj: dict[str, Any] = {"type": "end"}
    _put(obj, "comment", block.comment)
    return obj


def _enc_custom(block: Custom, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    return {
        "type": "custom",
        "pen": block.pen,
        "subtype": block.subtype,
        "payload": _b64e(block.payload),
    }


def _enc_unknown(block: UnknownBlock, on_issue: Callable[[str], None]) -> dict[str, Any]:
    del on_issue
    return {
        "type": "unknown",
        "block_type": block.block_type,
        "content": _b64e(block.content),
    }


_ENCODERS: dict[type[Block], Callable[[Any, Callable[[str], None]], dict[str, Any]]] = {
    FileHeader: _enc_file,
    Source: _enc_source,
    Decoder: _enc_decoder,
    Session: _enc_session,
    Participant: _enc_participant,
    SessionEnd: _enc_session_end,
    Record: _enc_record,
    Undecoded: _enc_undecoded,
    NameResolution: _enc_name,
    End: _enc_end,
    Custom: _enc_custom,
    UnknownBlock: _enc_unknown,
}


# --- Per-block decoders (JSON object -> block) --------------------------------


def _dec_file(reader: _ObjReader) -> FileHeader:
    version_major, version_minor = _parse_format(reader.require("format"))
    tick_hz = _parse_time_units(reader.require("time_units"))
    flags = FileFlags.SINGLE_CLOCK if _take_flag(reader, "single_clock") else FileFlags(0)
    return FileHeader(
        tick_hz=tick_hz,
        version_major=version_major,
        version_minor=version_minor,
        time_epoch=reader.take_int("time_epoch"),
        creator=reader.take_str("creator"),
        produced_by=reader.take_str("produced_by"),
        produced_at=reader.take_int("produced_at"),
        flags=flags,
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _take_flag(reader: _ObjReader, key: str) -> bool:
    value = reader.take(key)
    return value is not None and _dec_bool(value, key)


def _dec_source(reader: _ObjReader) -> Source:
    raw_kind = reader.require("kind")
    if isinstance(raw_kind, str):
        if raw_kind not in _LABEL_TO_KIND:
            msg = f"unknown source kind {raw_kind!r}"
            raise ValueError(msg)
        kind: SourceKind | int = _LABEL_TO_KIND[raw_kind]
    else:
        kind = _dec_int(raw_kind, "kind")
    return Source(
        source_id=reader.require_int("source_id"),
        kind=kind,
        uri=reader.take_str("uri"),
        digest=reader.take_str("digest"),
        link_type=reader.take_int("link_type"),
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_decoder(reader: _ObjReader) -> Decoder:
    return Decoder(
        decoder_id=reader.require_int("decoder_id"),
        name=reader.take_str("name"),
        version=reader.take_str("version"),
        params_digest=reader.take_str("params_digest"),
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_session(reader: _ObjReader) -> Session:
    flags = SessionFlags.SEQUENCED if _take_flag(reader, "sequenced") else SessionFlags(0)
    return Session(
        session_id=reader.require_int("session_id"),
        proto=reader.take_str("proto"),
        flow_key=reader.take_str("key"),
        flags=flags,
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_participant(reader: _ObjReader) -> Participant:
    raw_endpoint = reader.take("endpoint")
    if raw_endpoint is None:
        endpoints: tuple[str, ...] = ()
    elif isinstance(raw_endpoint, str):
        endpoints = (raw_endpoint,)
    elif isinstance(raw_endpoint, list):
        endpoints = tuple(_dec_str(entry, "endpoint") for entry in raw_endpoint)
    else:
        msg = f"endpoint must be a string or array, got {raw_endpoint!r}"
        raise ValueError(msg)
    raw_role = reader.take("tcp_role")
    if raw_role is None:
        tcp_role = None
    elif raw_role in _LABEL_TO_TCP_ROLE:
        tcp_role = _LABEL_TO_TCP_ROLE[raw_role]
    else:
        msg = f"unknown tcp_role {raw_role!r}"
        raise ValueError(msg)
    raw_origin = reader.take("origin")
    return Participant(
        session_id=reader.require_int("session_id"),
        participant_id=reader.require_int("pid"),
        endpoints=endpoints,
        isn=reader.take_int("isn"),
        identity=reader.take_str("identity"),
        tcp_role=tcp_role,
        origin=None if raw_origin is None else _origin_from_json(raw_origin),
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_session_end(reader: _ObjReader) -> SessionEnd:
    return SessionEnd(
        session_id=reader.require_int("session_id"),
        reason=reader.take_str("reason"),
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_record_flags(value: Any, on_issue: Callable[[str], None]) -> RecordFlags:
    if not isinstance(value, list):
        msg = f"record flags must be an array of tokens, got {value!r}"
        raise ValueError(msg)
    flags = RecordFlags(0)
    for token in value:
        flag = _TOKEN_TO_RECORD_FLAG.get(token)
        if flag is None:
            on_issue(f"unknown record flag token {token!r} was dropped")
        else:
            flags |= flag
    return flags


def _dec_record(reader: _ObjReader, on_issue: Callable[[str], None]) -> Record:
    raw_flags = reader.take("flags")
    raw_spans = reader.take("spans")
    if raw_spans is not None and not isinstance(raw_spans, list):
        msg = f"spans must be an array, got {raw_spans!r}"
        raise ValueError(msg)
    return Record(
        session_id=reader.require_int("session_id"),
        sender_pid=reader.require_int("sender_pid"),
        source_id=reader.require_int("source_id"),
        timestamp=reader.require_int("ts"),
        payload=_b64d(reader.require("payload"), "payload"),
        flags=RecordFlags(0) if raw_flags is None else _dec_record_flags(raw_flags, on_issue),
        seq_start=reader.take_int("seq_start"),
        ack=reader.take_int("ack"),
        ts_first=reader.take_int("ts_first"),
        spans=() if raw_spans is None else tuple(_span_from_json(entry) for entry in raw_spans),
        decoder_id=reader.take_int("decoder_id"),
        content_type=reader.take_str("content_type"),
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_undecoded(reader: _ObjReader) -> Undecoded:
    return Undecoded(
        source_id=reader.require_int("source_id"),
        session_id=reader.require_int("session_id"),
        participant_id=reader.require_int("pid"),
        off_start=reader.require_int("off_start"),
        off_end=reader.require_int("off_end"),
        reason=reader.take_str("reason"),
        decoder_id=reader.take_int("decoder_id"),
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_name(reader: _ObjReader) -> NameResolution:
    return NameResolution(
        session_id=reader.require_int("session_id"),
        participant_id=reader.require_int("pid"),
        label=reader.take_str("label"),
        kind=reader.take_str("kind"),
        comment=reader.take_str("comment"),
        extra_options=reader.options(),
    )


def _dec_end(reader: _ObjReader) -> End:
    return End(comment=reader.take_str("comment"), extra_options=reader.options())


def _dec_custom(reader: _ObjReader) -> Custom:
    return Custom(
        pen=reader.require_int("pen"),
        subtype=reader.require_int("subtype"),
        payload=_b64d(reader.require("payload"), "payload"),
    )


def _dec_unknown(reader: _ObjReader) -> UnknownBlock:
    return UnknownBlock(
        block_type=reader.require_int("block_type"),
        content=_b64d(reader.require("content"), "content"),
    )


_DECODERS: dict[str, Callable[[_ObjReader], Block]] = {
    "file": _dec_file,
    "source": _dec_source,
    "decoder": _dec_decoder,
    "session": _dec_session,
    "participant": _dec_participant,
    "session_end": _dec_session_end,
    "undecoded": _dec_undecoded,
    "name": _dec_name,
    "end": _dec_end,
    "custom": _dec_custom,
    "unknown": _dec_unknown,
}


# --- Public line-level codec ---------------------------------------------------


def block_to_obj(block: Block, *, on_issue: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Project one block to its JSONL object.

    Args:
        block: Any block from :mod:`zpf.blocks`.
        on_issue: Called with a message for each value that has no JSONL
            representation (e.g. a Record flags bit with no token, which is
            dropped). ``None`` ignores issues.

    Returns:
        The JSON-serializable object for the block's line.

    """
    encoder = _ENCODERS[type(block)]
    obj = encoder(block, on_issue or _ignore_issue)
    extras: tuple[RawOption, ...] = getattr(block, "extra_options", ())
    if extras:
        obj["options"] = _options_to_json(extras)
    return obj


def obj_to_block(obj: Mapping[str, Any], *, on_issue: Callable[[str], None] | None = None) -> Block:
    """Build a typed block from one JSONL object.

    Args:
        obj: A parsed JSONL line (must carry a known ``type`` string).
        on_issue: Called with a message for each value that has no binary
            representation (an unknown key, an unknown flag token), which is
            dropped. ``None`` ignores issues.

    Returns:
        The typed block.

    Raises:
        ValueError: If the object is malformed — an unknown ``type``, a
            missing body field, or a value of the wrong shape.
        EncodeError: If a value is out of range for its binary field.

    """
    handler = on_issue or _ignore_issue
    type_string = obj.get("type")
    if not isinstance(type_string, str):
        msg = "block object has no 'type' string"
        raise ValueError(msg)
    reader = _ObjReader(obj)
    if type_string == "record":
        block: Block = _dec_record(reader, handler)
    else:
        decoder = _DECODERS.get(type_string)
        if decoder is None:
            msg = f"unknown block type string {type_string!r}"
            raise ValueError(msg)
        block = decoder(reader)
    reader.finish(handler)
    return block


def dumps_block(block: Block, *, on_issue: Callable[[str], None] | None = None) -> str:
    """Serialize one block to its JSONL line (no trailing newline)."""
    obj = block_to_obj(block, on_issue=on_issue)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def loads_block(line: str, *, on_issue: Callable[[str], None] | None = None) -> Block:
    """Parse one JSONL line into a typed block.

    Raises:
        ValueError: If the line is not valid JSON or not a valid block
            object (see :func:`obj_to_block`).

    """
    obj = json.loads(line)
    if not isinstance(obj, dict):
        msg = "a JSONL line must hold a JSON object"
        raise ValueError(msg)
    return obj_to_block(obj, on_issue=on_issue)


# --- Stream level ---------------------------------------------------------------


class JsonlReader:
    """Iterate the blocks of a JSONL-projected ZPF file or text stream.

    Mirrors :class:`~zpf.binary.BlockReader`: single-pass, structural
    problems always raise, and expected imperfections are reported through
    status attributes unless ``strict`` is set. Blank lines are skipped (the
    specification's own examples contain them).

    Attributes:
        strict: Whether recoverable issues escalate to exceptions.
        complete: True once an ``end`` line was read.
        truncated: True if the stream ended inside the final line.
        diagnostics: Non-fatal conditions; ``offset`` is the 1-based line
            number.
        header: The File Header, available once the first line was read.

    """

    def __init__(
        self, source: str | os.PathLike[str] | IO[str], *, strict: bool = False
    ) -> None:
        if isinstance(source, (str, os.PathLike)):
            self._stream: IO[str] = open(source, encoding="utf-8")  # noqa: SIM115 -- closed by close()
            self._owns_stream = True
        else:
            self._stream = source
            self._owns_stream = False
        self.strict = strict
        self.complete = False
        self.truncated = False
        self.diagnostics: list[Diagnostic] = []
        self.header: FileHeader | None = None
        self._line_no = 0
        self._finished = False
        self._after_end = False

    def __enter__(self) -> JsonlReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying stream if this reader opened it."""
        if self._owns_stream:
            self._stream.close()

    @property
    def line_no(self) -> int:
        """The 1-based number of the last line read (0 before any read)."""
        return self._line_no

    def __iter__(self) -> JsonlReader:
        return self

    def __next__(self) -> Block:
        while True:
            if self._finished:
                raise StopIteration
            line = self._next_line()
            if line is None:
                self._finish_at_eof()
                raise StopIteration
            if self._after_end:
                self._report_trailing()
                raise StopIteration
            block = self._parse_line(line)
            if block is not None:
                return block

    def _next_line(self) -> str | None:
        """Return the next non-blank line, or ``None`` at end of stream."""
        while True:
            line = self._stream.readline()
            if not line:
                return None
            self._line_no += 1
            if line.strip():
                return line

    def _finish_at_eof(self) -> None:
        self._finished = True
        if self._line_no == 0:
            msg = "empty file: missing the 'file' header line"
            raise StructuralError(msg)

    def _report_trailing(self) -> None:
        self._finished = True
        message = f"line {self._line_no}: content after the 'end' line is not blocks"
        self.diagnostics.append(Diagnostic(self._line_no, "trailing-lines", message))
        if self.strict:
            raise SemanticError(message)

    def _parse_line(self, line: str) -> Block | None:
        """Parse one line; return ``None`` when the line was isolated."""
        first = self.header is None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            self._handle_undecodable_line(exc)
            raise StopIteration from None
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
            msg = f"line {self._line_no}: not a block object with a 'type' string"
            raise StructuralError(msg)
        if first and obj["type"] != "file":
            msg = f"line {self._line_no}: first block must be 'file', got {obj['type']!r}"
            raise StructuralError(msg)
        if not first and obj["type"] == "file":
            msg = f"line {self._line_no}: second 'file' line; a file has exactly one"
            raise StructuralError(msg)
        try:
            block = obj_to_block(obj, on_issue=self._issue)
        except (ValueError, EncodeError) as exc:
            if first:
                msg = f"line 1: invalid file header: {exc}"
                raise StructuralError(msg) from exc
            if self.strict:
                msg = f"line {self._line_no}: {exc}"
                raise SemanticError(msg) from exc
            self.diagnostics.append(Diagnostic(self._line_no, "invalid-line", str(exc)))
            return None
        return self._admit(block)

    def _admit(self, block: Block) -> Block:
        if isinstance(block, FileHeader):
            unsupported = unsupported_version(block.version_major, block.version_minor)
            if unsupported is not None:
                raise StructuralError(unsupported)
            self.header = block
        elif isinstance(block, End):
            self.complete = True
            self._after_end = True
        return block

    def _handle_undecodable_line(self, exc: json.JSONDecodeError) -> None:
        """Handle a non-JSON line: truncation if final, structural otherwise."""
        if self._stream.readline():
            msg = f"line {self._line_no}: invalid JSON: {exc}"
            raise StructuralError(msg) from exc
        self.truncated = True
        self._finished = True
        message = f"line {self._line_no}: stream ends inside a line"
        self.diagnostics.append(Diagnostic(self._line_no, "truncated", message))
        if self.strict:
            raise TruncatedError(message) from exc

    def _issue(self, message: str) -> None:
        if self.strict:
            raise SemanticError(f"line {self._line_no}: {message}")
        self.diagnostics.append(Diagnostic(self._line_no, "jsonl-edge", message))


class JsonlWriter:
    """Write blocks as JSONL lines to a file or text stream.

    Mirrors :class:`~zpf.binary.BlockWriter`: the first block must be a
    :class:`~zpf.blocks.FileHeader`, only one is allowed, and nothing can
    follow an :class:`~zpf.blocks.End` block. Values with no JSONL
    representation are dropped with a diagnostic, or raise under ``strict``.
    Pass ``check=True`` to also run every written block through a
    :class:`~zpf.conformance.ConformanceChecker` (off by default, so tools
    may re-emit imperfect data).

    Attributes:
        strict: Whether representation issues raise instead of reporting.
        diagnostics: Non-fatal conditions; ``offset`` is the 1-based line
            number.

    """

    def __init__(
        self,
        sink: str | os.PathLike[str] | IO[str],
        *,
        strict: bool = False,
        check: bool = False,
    ) -> None:
        if isinstance(sink, (str, os.PathLike)):
            self._stream: IO[str] = open(sink, "w", encoding="utf-8", newline="\n")  # noqa: SIM115 -- closed by close()
            self._owns_stream = True
        else:
            self._stream = sink
            self._owns_stream = False
        self.strict = strict
        self.diagnostics: list[Diagnostic] = []
        self._line_no = 0
        self._closed = False
        self._ended = False
        self._checker = ConformanceChecker() if check else None

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def write(self, block: Block) -> int:
        """Write one block as one line.

        Args:
            block: The block to write.

        Returns:
            The 1-based line number of the written line.

        Raises:
            ZpfError: If the writer is closed.
            StructuralError: If the block would make the file ill-formed
                (missing/duplicate File Header, block after End).
            SemanticError: In strict mode, if a value has no JSONL
                representation.

        """
        if self._closed:
            msg = "writer is closed"
            raise ZpfError(msg)
        if self._ended:
            msg = "cannot write a block after the End block"
            raise StructuralError(msg)
        is_header = isinstance(block, FileHeader)
        if self._line_no == 0 and not is_header:
            msg = f"first block must be a File Header, not {type(block).__name__}"
            raise StructuralError(msg)
        if self._line_no > 0 and is_header:
            msg = "a file has exactly one File Header, as its first block"
            raise StructuralError(msg)
        if self._checker is not None:
            self._checker.observe(block)
        obj = block_to_obj(block, on_issue=self._issue)
        self._line_no += 1
        self._stream.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")
        if isinstance(block, End):
            self._ended = True
        return self._line_no

    def close(self) -> None:
        """Flush and close the underlying stream if this writer opened it."""
        if self._closed:
            return
        self._closed = True
        self._stream.flush()
        if self._owns_stream:
            self._stream.close()

    def _issue(self, message: str) -> None:
        if self.strict:
            raise SemanticError(f"line {self._line_no + 1}: {message}")
        self.diagnostics.append(Diagnostic(self._line_no + 1, "jsonl-edge", message))


# --- Whole-file converters -------------------------------------------------------


def binary_to_jsonl(
    source: str | os.PathLike[str] | IO[bytes],
    sink: str | os.PathLike[str] | IO[str],
    *,
    strict: bool = False,
) -> list[Diagnostic]:
    """Convert a binary ``.zpf`` file to its JSONL projection.

    The conversion is semantically lossless: every field value survives,
    but binary padding/option-order details are not represented, so
    converting back yields a canonically re-encoded (not byte-identical)
    file.

    Args:
        source: Binary input path or stream.
        sink: Text output path or stream.
        strict: Escalate recoverable issues to exceptions on both ends.

    Returns:
        The diagnostics collected by the reader and the writer.

    """
    with BlockReader(source, strict=strict) as reader, JsonlWriter(sink, strict=strict) as writer:
        for block in reader:
            writer.write(block)
        return [*reader.diagnostics, *writer.diagnostics]


def jsonl_to_binary(
    source: str | os.PathLike[str] | IO[str],
    sink: str | os.PathLike[str] | IO[bytes],
    *,
    strict: bool = False,
) -> list[Diagnostic]:
    """Convert a JSONL projection back to a binary ``.zpf`` file.

    Args:
        source: Text input path or stream.
        sink: Binary output path or stream.
        strict: Escalate recoverable issues to exceptions on both ends.

    Returns:
        The diagnostics collected by the reader.

    """
    with JsonlReader(source, strict=strict) as reader, BlockWriter(sink) as writer:
        for block in reader:
            writer.write(block)
        return list(reader.diagnostics)

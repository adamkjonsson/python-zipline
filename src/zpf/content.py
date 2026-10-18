r"""The ``content_type`` label: its grammar, and the normative ``prim:`` decode.

A record's ``content_type`` names what its payload *is*, as ``scheme:value``,
in one of four schemes. The specification settles ``prim:`` completely: its
vocabulary is closed (``u8``…``u64``, ``i8``…``i64``, ``bytes``), its
integers are little-endian, their signedness comes from the token's ``u``/``i``
prefix, and the width MUST equal ``payload_len``. ``net:`` (since ``0.22``) is
closed too, with ``net:ip`` and ``net:ethernet``: the payload is exactly one
packet or frame, meaning what the pcap link type in :data:`NET_LINK_TYPES`
means, and the format writes no packet grammar of its own. ``mime:`` says
only that the value is an IANA media type; ``dec:`` is a type private to the
record's decoder, meaning whatever that decoder documents. Only ``prim:``
can be turned into a Python value without going beyond the format, so only
``prim:`` is decoded here.

The governing sentence, which shapes every signature in this module:

    the bytes always stay the source of truth — the label never replaces them.

So two fallback rules are normative, and :func:`decode_prim` implements them
by returning ``None`` rather than raising: an **unknown scheme or token is
opaque** (not an error — the payload is simply bytes), and a ``prim:`` **width
that disagrees with the payload** MUST be treated as unknown, with the reader
forbidden to "pad, truncate, or reinterpret". Callers therefore get the
spec's fallback by construction — the worst they can do is ignore a ``None``.

A label is carried as written and compared exactly — no normalizing, no case
folding — with one deliberate exception: :class:`ContentRegistry` matches
``mime:`` media types case-insensitively and ignores their parameters,
because IANA defines type and subtype that way.

That registry is where anything *beyond* the format lives: ``mime:`` and
``dec:`` mean nothing the format defines, so interpreting them is a
caller-supplied claim, dispatched by :meth:`zpf.FileReader.content` and
never by this module's normative functions.

This module is deliberately dependency-free — it works on :class:`str` and
:class:`bytes`, never on blocks — so the block layer can build on it.

Example:
    >>> ContentType.parse("prim:u32")
    ContentType(scheme='prim', value='u32')
    >>> decode_prim(b"\xd2\x04\x00\x00", "u32")
    1234
    >>> decode_prim(b"\xd2\x04\x00", "u32") is None  # width disagrees
    True

"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

ContentHandler: TypeAlias = Callable[[bytes], object]
"""What a caller registers to interpret one advisory label — bytes in, a value out."""

_SCHEME_MIME = "mime"
_SCHEME_DEC = "dec"
_SCHEME_NET = "net"

PRIM_WIDTHS: Mapping[str, int] = MappingProxyType({
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "u64": 8,
    "i64": 8,
})
"""The closed ``prim:`` integer vocabulary, token to its required width."""

PRIM_BYTES = "bytes"
"""The one ``prim:`` token that is not an integer: uninterpreted bytes."""

NET_LINK_TYPES: Mapping[str, int] = MappingProxyType({
    "ip": 101,
    "ethernet": 1,
})
"""The closed ``net:`` vocabulary, token to the pcap link type it means.

A ``net:ip`` payload is one IP packet, header first, its version nibble saying
IPv4 or IPv6 — ``LINKTYPE_RAW`` (101). A ``net:ethernet`` payload is one
Ethernet frame from the destination address to the end of the payload, with
no preamble, SFD **or FCS** — ``LINKTYPE_ETHERNET`` (1), less the frame check
sequence that link type leaves optional. So a payload can be handed to
anything that reads a capture of that link type.
"""

NET_IP = "net:ip"
"""The label of a record whose payload is exactly one IP packet."""

NET_ETHERNET = "net:ethernet"
"""The label of a record whose payload is exactly one Ethernet frame, without FCS."""


@dataclass(frozen=True)
class ContentType:
    """A parsed ``content_type`` label — its scheme and the rest.

    Attributes:
        scheme: The part before the first colon: ``"prim"``, ``"net"``,
            ``"mime"``, ``"dec"``, or any other (an unknown scheme is opaque,
            not an error). Empty when the label carries no colon at all, since
            such a label names no scheme.
        value: Everything after the first colon, unmodified — a ``prim:``
            token, a media type with its parameters, or a decoder-private
            type name. Colons within it are not separators.

    Example:
        >>> ContentType.parse("mime:text/plain; charset=utf-8")
        ContentType(scheme='mime', value='text/plain; charset=utf-8')

    """

    scheme: str
    value: str

    @classmethod
    def parse(cls, label: str) -> ContentType:
        """Split a ``content_type`` label into its scheme and value.

        Total by design: every string parses, because the format's fallback
        for a label a reader does not understand is to treat the payload as
        opaque bytes, never to reject the record.

        Args:
            label: The label as written in the file.

        Returns:
            The parsed label. A label with no colon yields an empty
            ``scheme`` and keeps the whole label as ``value``.

        """
        scheme, colon, value = label.partition(":")
        if not colon:
            return cls(scheme="", value=label)
        return cls(scheme=scheme, value=value)

    @property
    def is_prim(self) -> bool:
        """Whether this label uses the fully spec-defined ``prim:`` scheme."""
        return self.scheme == "prim"

    @property
    def is_net(self) -> bool:
        """Whether this label uses the spec-defined ``net:`` packet scheme."""
        return self.scheme == _SCHEME_NET

    @property
    def link_type(self) -> int | None:
        """The pcap link type a ``net:`` label means, or None.

        ``None`` for any other scheme, and for a ``net:`` token this version
        does not define — which a reader treats as opaque, the way it treats
        an unknown scheme, rather than as an error.

        Example:
            >>> ContentType.parse("net:ip").link_type
            101

        """
        return NET_LINK_TYPES.get(self.value) if self.is_net else None


def prim_fault(payload: bytes, token: str) -> str | None:
    """Explain why a ``prim:`` token cannot be honoured for this payload.

    The complement of :func:`decode_prim`: this returns a reason exactly
    when that returns ``None``, which is what keeps the conformance
    checker's finding and the decode's fallback from ever disagreeing.

    Args:
        payload: The record's payload bytes.
        token: A ``prim:`` label's value, e.g. ``"u32"``.

    Returns:
        A phrase completing "content_type ``<label>`` …", or ``None`` when
        the label can be honoured.

    Example:
        >>> prim_fault(b"abc", "u32")
        'requires payload_len 4, got 3'

    """
    if token == PRIM_BYTES:
        return None
    width = PRIM_WIDTHS.get(token)
    if width is None:
        return "is not a legal prim: token"
    if len(payload) != width:
        return f"requires payload_len {width}, got {len(payload)}"
    return None


def net_fault(token: str) -> str | None:
    """Explain why a ``net:`` token cannot be honoured, or None if it can.

    The ``net:`` vocabulary is closed: a writer MUST use one of
    :data:`NET_LINK_TYPES`, and a reader treats any other token as opaque.
    Unlike ``prim:``, a ``net:`` token binds no length and nothing checks the
    payload against it — whether it is an IP packet is protocol parsing,
    which the format does not do — so the token is the only thing to check.

    Args:
        token: A ``net:`` label's value, e.g. ``"ip"``.

    Returns:
        A phrase completing "content_type ``<label>`` …", or ``None``.

    Example:
        >>> net_fault("ipv4")
        'is not a legal net: token'

    """
    return None if token in NET_LINK_TYPES else "is not a legal net: token"


def decode_prim(payload: bytes, token: str) -> int | bytes | None:
    r"""Interpret a payload as a ``prim:`` token says, or report it opaque.

    Fully normative: the integer tokens are little-endian, signed iff the
    token starts with ``i``, and the token's width MUST equal the payload's
    length. A disagreement is not repaired — the specification forbids
    padding, truncating, and reinterpreting — it is reported as opaque.

    Args:
        payload: The record's payload bytes.
        token: A ``prim:`` label's value, e.g. ``"u32"`` or ``"bytes"``.

    Returns:
        An :class:`int` for an integer token whose width matches; the
        ``payload`` itself for ``"bytes"``; ``None`` when the label cannot
        be honoured (unknown token, or a width the payload contradicts),
        which is the caller's cue to fall back to the raw bytes.

    Example:
        >>> decode_prim(b"\xff\xff", "i16")
        -1

    """
    if prim_fault(payload, token) is not None:
        return None
    if token == PRIM_BYTES:
        return payload
    return int.from_bytes(payload, "little", signed=token.startswith("i"))


def media_type_of(value: str) -> str:
    """Return a ``mime:`` label's media type, without its parameters.

    Args:
        value: A ``mime:`` label's value, e.g.
            ``"text/plain; charset=utf-8"``.

    Returns:
        The bare media type, lowercased — IANA defines type and subtype as
        case-insensitive, so ``"Text/Plain"`` and ``"text/plain"`` are the
        same type. Parameters are dropped, not interpreted.

    Example:
        >>> media_type_of("Text/Plain; charset=utf-8")
        'text/plain'

    """
    return value.partition(";")[0].strip().lower()


class ContentRegistry:
    """Caller-supplied interpretations of the advisory ``content_type`` schemes.

    ⚠ **Beyond the standard.** The format defines ``mime:`` only as "an IANA
    media type", ``dec:`` as "a type private to the record's decoder,
    meaning whatever that decoder documents", and ``net:`` as one packet of a
    pcap link type — it says nothing about turning any of them into a value.
    This class supplies the *dispatch* for that, never the meaning: what a
    label means is your handler's claim, not the library's, and not the
    format's. ``prim:`` is the opposite case — fully normative, built in, and
    never routed through a registry.

    A handler takes the payload bytes and returns whatever it likes; its
    exceptions propagate to the caller unchanged, since a handler that fails
    is a bug or corrupt input, not a fallback condition.

    Registration is by media type for ``mime:`` (parameters such as
    ``charset=`` are not part of the key), by the **decoder's name** plus
    token for ``dec:`` — the name is what the format namespaces a
    decoder-private type by, not the decoder's id or version — and by token
    alone for ``net:``, whose meaning is the format's and so the same whoever
    wrote the record. Registering the same key twice replaces the handler.

    Pass a registry to :func:`zpf.open` to have
    :meth:`zpf.FileReader.content` use it.

    Example:
        >>> import json
        >>> registry = ContentRegistry()
        >>> registry.register_mime("application/json", json.loads)
        >>> registry.register_dec("http/1.1", "request", bytes.splitlines)
        >>> registry.register_net("ip", lambda packet: packet[0] >> 4)

    """

    def __init__(self) -> None:
        self._mime: dict[str, ContentHandler] = {}
        self._dec: dict[tuple[str, str], ContentHandler] = {}
        self._net: dict[str, ContentHandler] = {}

    def register_mime(self, media_type: str, handler: ContentHandler) -> None:
        """Register a handler for one IANA media type.

        Args:
            media_type: The media type, e.g. ``"application/json"``.
                Matching ignores a label's parameters, so a handler for
                ``"text/plain"`` also serves
                ``mime:text/plain; charset=utf-8``.
            handler: Called with the record's payload bytes.

        Raises:
            ValueError: If the media type is empty, or carries parameters
                (they are not part of the key — one handler serves them all).

        """
        if ";" in media_type:
            msg = (
                f"media type {media_type!r} carries parameters; register "
                f"{media_type_of(media_type)!r} instead — it serves every parameter set"
            )
            raise ValueError(msg)
        key = media_type_of(media_type)
        if not key:
            msg = "media type must not be empty"
            raise ValueError(msg)
        self._mime[key] = handler

    def register_dec(self, decoder_name: str, token: str, handler: ContentHandler) -> None:
        """Register a handler for one decoder's private type.

        Args:
            decoder_name: The producing decoder's ``name``, e.g.
                ``"http/1.1"`` — the namespace the format gives ``dec:``
                tokens. Matched exactly: these names are not case-insensitive
                identifiers the way media types are.
            token: The ``dec:`` label's token, e.g. ``"request"``.
            handler: Called with the record's payload bytes.

        Raises:
            ValueError: If the decoder name or the token is empty; neither
                could ever match a record.

        """
        if not decoder_name or not token:
            msg = "a dec: handler needs both a decoder name and a token"
            raise ValueError(msg)
        self._dec[(decoder_name, token)] = handler

    def register_net(self, token: str, handler: ContentHandler) -> None:
        """Register a handler for one ``net:`` link type.

        One handler serves every decoder's records: a ``net:`` token means
        the same packet whoever wrote it, which is what makes it the label a
        generic stage matches — a sessionizer reading the packets of any
        tunnel decoder, say.

        Args:
            token: A token of :data:`NET_LINK_TYPES`, e.g. ``"ip"``.
            handler: Called with the record's payload bytes: one whole
                packet or frame.

        Raises:
            ValueError: If the token is not one this version defines. A
                reader treats such a label as opaque, so a handler for it
                would interpret what the format says not to.

        """
        fault = net_fault(token)
        if fault is not None:
            msg = f"net:{token} {fault}; this version defines {sorted(NET_LINK_TYPES)}"
            raise ValueError(msg)
        self._net[token] = handler

    def handler(
        self, label: ContentType, *, decoder_name: str | None = None
    ) -> ContentHandler | None:
        """Return the handler for a label, or None if nothing is registered.

        Args:
            label: The record's parsed ``content_type``.
            decoder_name: The ``name`` of the decoder that produced the
                record, needed to namespace a ``dec:`` token. ``None`` when
                the record is not decoded, or its decoder declared no name —
                in which case a ``dec:`` label cannot be resolved at all.

        Returns:
            The registered handler, or ``None`` — for an unregistered key,
            for ``prim:`` (normative, never dispatched here), and for any
            other scheme, which the format calls opaque.

        """
        if label.scheme == _SCHEME_MIME:
            return self._mime.get(media_type_of(label.value))
        if label.scheme == _SCHEME_DEC and decoder_name is not None:
            return self._dec.get((decoder_name, label.value))
        if label.scheme == _SCHEME_NET:
            return self._net.get(label.value)
        return None

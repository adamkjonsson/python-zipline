"""Semantic conformance checking for block streams.

The specification's Conformance section states writer obligations that go
beyond well-formed bytes: declare-before-use, id uniqueness, session
lifetime, per-participant record order, and file-kind purity (a file is
raw, a decode stage, or a pass-through transform — never a mix).
:class:`ConformanceChecker` implements them as a single-pass observer over
a block sequence, raising :class:`~zpf.errors.SemanticError` on the first
violation. It is used by the ergonomic writer (always), by the flat
writers when constructed with ``check=True``, and can be run standalone
over any block iterable.

Memory stays bounded on unbounded streams: per-session state is freed at
the session's Session End; only the set of ended session ids is retained
(required to police the nothing-after-Session-End rule).

Not checked here (out of single-pass reach): that a SEQUENCED session's
stored order really is a causal linearization (needs the merge algorithm),
and the decode-stage coverage guarantee (a whole-file property, enforced by
the transform helpers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from zpf.blocks import (
    Block,
    Decoder,
    End,
    FileHeader,
    NameResolution,
    Participant,
    Record,
    Session,
    SessionEnd,
    Source,
    SourceKind,
    Undecoded,
)
from zpf.errors import SemanticError
from zpf.order import seq_leq

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from zpf.blocks import Span

_RECORD_RESERVED_FLAGS = 0xFF20
_FILE_RESERVED_FLAGS = 0xFFFE  # everything above SINGLE_CLOCK
_SESSION_RESERVED_FLAGS = 0xFFFE  # everything above SEQUENCED

_PRIM_WIDTHS = {
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "u64": 8,
    "i64": 8,
}

_RAW = "raw"
_DECODE = "decode-stage"
_PASS_THROUGH = "pass-through"


@dataclass
class _SessionState:
    """Live per-session bookkeeping, freed at Session End."""

    last_seq: dict[int, int | None] = field(default_factory=dict)  # pid -> last seq_start


class ConformanceChecker:
    """Validate a block stream against the specification's semantic tier.

    Feed blocks in file order to :meth:`observe`; the first violation
    raises :class:`~zpf.errors.SemanticError`. The file kind (raw,
    decode-stage, or pass-through) is inferred from the stream and locked
    at the first distinguishing block; the error message for a later
    conflict names both that block and the offending one.

    Example:
        >>> checker = ConformanceChecker()
        >>> for block in blocks:
        ...     checker.observe(block)

    """

    def __init__(self) -> None:
        self._header: FileHeader | None = None
        self._sources: dict[int, SourceKind | int] = {}
        self._decoders: set[int] = set()
        self._live: dict[int, _SessionState] = {}
        self._ended: set[int] = set()
        self._kind: str | None = None
        self._kind_reason = ""
        self._orphan_participants: list[str] = []  # declared without origin, kind still open
        self._file_ended = False
        self._dispatch: dict[type[Block], Callable[[Any], None]] = {
            FileHeader: self._on_file_header,
            Source: self._on_source,
            Decoder: self._on_decoder,
            Session: self._on_session,
            Participant: self._on_participant,
            SessionEnd: self._on_session_end,
            Record: self._on_record,
            Undecoded: self._on_undecoded,
            NameResolution: self._on_name,
            End: self._on_end,
        }

    @property
    def file_kind(self) -> str | None:
        """The inferred file kind, once a block has locked it.

        One of ``"raw"``, ``"decode-stage"``, ``"pass-through"``, or
        ``None`` while the stream has not yet contained a distinguishing
        block.
        """
        return self._kind

    def observe(self, block: Block) -> None:
        """Check one block, in file order.

        Args:
            block: The next block of the stream.

        Raises:
            SemanticError: On the stream's first conformance violation.

        """
        if self._file_ended:
            msg = f"{_describe(block)} appears after the End block"
            raise SemanticError(msg)
        if self._header is None and not isinstance(block, FileHeader):
            msg = f"first block must be a File Header, got {_describe(block)}"
            raise SemanticError(msg)
        handler = self._dispatch.get(type(block))
        if handler is not None:
            handler(block)

    def check(self, blocks: Iterable[Block]) -> None:
        """Check a whole block sequence (convenience for standalone use).

        Raises:
            SemanticError: On the sequence's first conformance violation.

        """
        for block in blocks:
            self.observe(block)

    # --- Per-block handlers ----------------------------------------------

    def _on_file_header(self, block: FileHeader) -> None:
        if self._header is not None:
            msg = "second File Header; a file has exactly one, as its first block"
            raise SemanticError(msg)
        if int(block.flags) & _FILE_RESERVED_FLAGS:
            msg = f"File Header flags 0x{int(block.flags):04X} set reserved bits (must be 0)"
            raise SemanticError(msg)
        self._header = block

    def _on_source(self, block: Source) -> None:
        if block.source_id in self._sources:
            msg = f"source id {block.source_id} declared twice"
            raise SemanticError(msg)
        self._sources[block.source_id] = block.kind

    def _on_decoder(self, block: Decoder) -> None:
        if block.decoder_id in self._decoders:
            msg = f"decoder id {block.decoder_id} declared twice"
            raise SemanticError(msg)
        self._decoders.add(block.decoder_id)

    def _on_session(self, block: Session) -> None:
        if block.session_id in self._live or block.session_id in self._ended:
            msg = f"session id {block.session_id} declared twice"
            raise SemanticError(msg)
        if int(block.flags) & _SESSION_RESERVED_FLAGS:
            msg = f"session {block.session_id} flags set reserved bits (must be 0)"
            raise SemanticError(msg)
        self._live[block.session_id] = _SessionState()

    def _on_participant(self, block: Participant) -> None:
        state = self._require_live_session(block.session_id, _describe(block))
        if block.participant_id in state.last_seq:
            msg = (
                f"participant {block.participant_id} of session {block.session_id} "
                "declared twice"
            )
            raise SemanticError(msg)
        state.last_seq[block.participant_id] = None
        if block.origin is not None:
            origin_kind = self._require_source(block.origin.source_id, _describe(block))
            if origin_kind != SourceKind.ZPF_INPUT:
                msg = f"{_describe(block)} origin must reference a zpf-input source"
                raise SemanticError(msg)
            self._lock_kind(_PASS_THROUGH, f"{_describe(block)} (carries origin)")
        elif self._kind == _PASS_THROUGH:
            msg = (
                f"{_describe(block)} carries no origin, but {self._kind_reason} "
                "made this a pass-through file (every participant must map to its input)"
            )
            raise SemanticError(msg)
        else:
            self._orphan_participants.append(_describe(block))

    def _on_session_end(self, block: SessionEnd) -> None:
        self._require_live_session(block.session_id, _describe(block))
        del self._live[block.session_id]
        self._ended.add(block.session_id)

    def _on_record(self, block: Record) -> None:
        described = _describe(block)
        state = self._require_live_session(block.session_id, described)
        if block.sender_pid not in state.last_seq:
            msg = f"{described} names undeclared sender participant {block.sender_pid}"
            raise SemanticError(msg)
        self._classify_record(block, described)
        self._check_record_options(block, described)
        self._check_record_order(block, state, described)

    def _on_undecoded(self, block: Undecoded) -> None:
        described = _describe(block)
        kind = self._require_source(block.source_id, described)
        if kind != SourceKind.ZPF_INPUT:
            msg = f"{described} must reference a zpf-input source, not {kind!r}"
            raise SemanticError(msg)
        if block.decoder_id is not None and block.decoder_id not in self._decoders:
            msg = f"{described} names undeclared decoder {block.decoder_id}"
            raise SemanticError(msg)
        self._lock_kind(_DECODE, described)

    def _on_name(self, block: NameResolution) -> None:
        state = self._require_live_session(block.session_id, _describe(block))
        if block.participant_id not in state.last_seq:
            msg = f"{_describe(block)} names undeclared participant {block.participant_id}"
            raise SemanticError(msg)

    def _on_end(self, block: End) -> None:
        del block
        self._file_ended = True

    # --- Record helpers ----------------------------------------------------

    def _classify_record(self, block: Record, described: str) -> None:
        """Lock the file kind implied by this record and check its references."""
        source_kind = self._require_source(block.source_id, described)
        if block.decoder_id is not None:
            if block.decoder_id not in self._decoders:
                msg = f"{described} names undeclared decoder {block.decoder_id}"
                raise SemanticError(msg)
            if source_kind != SourceKind.ZPF_INPUT:
                msg = f"{described} is decoded, so it must reference a zpf-input source"
                raise SemanticError(msg)
            self._lock_kind(_DECODE, f"{described} (carries decoder_id)")
        elif source_kind == SourceKind.CAPTURE:
            self._lock_kind(_RAW, f"{described} (byte run from a capture source)")
        elif source_kind == SourceKind.ZPF_INPUT:
            if block.spans:
                msg = f"{described} is a pass-through record and must not carry spans"
                raise SemanticError(msg)
            self._lock_kind(_PASS_THROUGH, f"{described} (byte run from a zpf-input source)")
        else:
            msg = f"{described} references source {block.source_id} of unknown kind {source_kind}"
            raise SemanticError(msg)
        self._check_spans(block.spans, decoded=block.decoder_id is not None, described=described)

    def _check_spans(self, spans: tuple[Span, ...], *, decoded: bool, described: str) -> None:
        wanted = SourceKind.ZPF_INPUT if decoded else SourceKind.CAPTURE
        for span in spans:
            kind = self._require_source(span.source_id, f"{described} span")
            if kind != wanted:
                msg = f"{described} span must reference a {wanted.name} source"
                raise SemanticError(msg)

    def _check_record_options(self, block: Record, described: str) -> None:
        if int(block.flags) & _RECORD_RESERVED_FLAGS:
            msg = f"{described} flags 0x{int(block.flags):04X} set reserved bits (must be 0)"
            raise SemanticError(msg)
        content_type = block.content_type
        if content_type is None or not content_type.startswith("prim:"):
            return
        token = content_type[len("prim:") :]
        if token == "bytes":
            return
        width = _PRIM_WIDTHS.get(token)
        if width is None:
            msg = f"{described} content_type {content_type!r} is not a legal prim: token"
            raise SemanticError(msg)
        if len(block.payload) != width:
            msg = (
                f"{described} content_type {content_type!r} requires payload_len {width}, "
                f"got {len(block.payload)}"
            )
            raise SemanticError(msg)

    def _check_record_order(self, block: Record, state: _SessionState, described: str) -> None:
        if block.seq_start is None:
            return
        last = state.last_seq[block.sender_pid]
        if last is not None and not seq_leq(last, block.seq_start):
            msg = (
                f"{described} seq_start {block.seq_start} precedes the participant's "
                f"previous record ({last}); records must be stored in seq_start order"
            )
            raise SemanticError(msg)
        state.last_seq[block.sender_pid] = block.seq_start

    # --- Shared helpers -----------------------------------------------------

    def _require_live_session(self, session_id: int, described: str) -> _SessionState:
        state = self._live.get(session_id)
        if state is None:
            if session_id in self._ended:
                msg = f"{described} references session {session_id} after its Session End"
            else:
                msg = f"{described} references undeclared session {session_id}"
            raise SemanticError(msg)
        return state

    def _require_source(self, source_id: int, described: str) -> SourceKind | int:
        kind = self._sources.get(source_id)
        if kind is None:
            msg = f"{described} references undeclared source {source_id}"
            raise SemanticError(msg)
        return kind

    def _lock_kind(self, kind: str, reason: str) -> None:
        """Lock the file kind, or verify it matches the already-locked one."""
        if self._kind is None:
            self._kind = kind
            self._kind_reason = reason
            if kind != _RAW:
                self._require_derived_header(reason)
            if kind == _PASS_THROUGH and self._orphan_participants:
                msg = (
                    f"{self._orphan_participants[0]} carries no origin, but {reason} "
                    "made this a pass-through file (every participant must map to its input)"
                )
                raise SemanticError(msg)
            self._orphan_participants.clear()
        elif self._kind != kind:
            msg = (
                f"{reason} implies a {kind} file, but {self._kind_reason} "
                f"already made it {self._kind} (a file is exactly one kind)"
            )
            raise SemanticError(msg)

    def _require_derived_header(self, reason: str) -> None:
        header = self._header
        if header is None or header.produced_by is None or header.produced_at is None:
            msg = (
                f"{reason} makes this a derived file, whose File Header must carry "
                "produced_by and produced_at"
            )
            raise SemanticError(msg)


def _describe(block: Block) -> str:
    """Return a short human label for a block, for error messages."""
    name = type(block).__name__
    session_id = getattr(block, "session_id", None)
    if isinstance(block, Record):
        return f"{name}(session {block.session_id}, sender {block.sender_pid})"
    if isinstance(block, Participant):
        return f"{name}(session {block.session_id}, pid {block.participant_id})"
    if session_id is not None:
        return f"{name}(session {session_id})"
    return name

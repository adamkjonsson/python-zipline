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

Findings come in two strengths. Most are *isolating*: the block cannot be
made sense of, so a lenient reader drops it. A few bind the writer only —
the specification tells a reader that meets them to ignore the offending
label and keep the bytes — and those raise
:class:`~zpf.errors.AdvisoryError`, a :class:`~zpf.errors.SemanticError`
subclass, so writers still refuse the block while a lenient reader can
report it and hand the block over. Two rules are advisory today: reserved
flag bits set in any flags field (the format gives them no meaning a reader
could act on, so ignoring them is the only reading available), and the
``prim:`` content-type ones (illegal token, width against ``payload_len``
— the label grammar and the vocabulary they check against live in
:mod:`zpf.content`). A block with several such findings reports them all in
one message.

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
    REASON_CLASSES,
    UNDECODED_REASONS,
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
from zpf.content import ContentType, prim_fault
from zpf.errors import AdvisoryError, SemanticError
from zpf.order import seq_leq

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from zpf.blocks import Span

# Reserved flag bits are deliberately *not* checked. The specification puts a
# nonzero reserved field alongside an unknown block type and an unknown option
# id as part of the extension mechanism — "not a violation ... the normal,
# conformant path" — and requires the bit to survive a round-trip without
# being interpreted. Diagnosing one would report conformant data as suspect.

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
        self._advisory: list[str] = []  # findings for the block being observed
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
            AdvisoryError: If the block's only violation is one a reader is
                told to read past (see the module docstring). The block has
                been fully accounted for when this is raised, so a lenient
                reader may keep it.
            SemanticError: On the stream's first isolating violation.

        """
        if self._file_ended:
            msg = f"{_describe(block)} appears after the End block"
            raise SemanticError(msg)
        if self._header is None and not isinstance(block, FileHeader):
            msg = f"first block must be a File Header, got {_describe(block)}"
            raise SemanticError(msg)
        self._advisory.clear()
        handler = self._dispatch.get(type(block))
        if handler is not None:
            handler(block)
        # Advisory findings are raised only here, once the handler has
        # returned: a lenient reader keeps such a block, so the checker must
        # already have absorbed it. An isolating violation raised inside the
        # handler skips this, and its notes go with the dropped block.
        if self._advisory:
            raise AdvisoryError("; ".join(self._advisory))

    def check(self, blocks: Iterable[Block]) -> None:
        """Check a whole block sequence (convenience for standalone use).

        Raises:
            AdvisoryError: On the sequence's first advisory finding.
            SemanticError: On the sequence's first isolating violation.

        """
        for block in blocks:
            self.observe(block)

    # --- Per-block handlers ----------------------------------------------

    def _on_file_header(self, block: FileHeader) -> None:
        if self._header is not None:
            msg = "second File Header; a file has exactly one, as its first block"
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
        self._live[block.session_id] = _SessionState()

    def _on_participant(self, block: Participant) -> None:
        state = self._require_live_session(block.session_id, _describe(block))
        if block.participant_id in state.last_seq:
            msg = (
                f"participant {block.participant_id} of session {block.session_id} "
                "declared twice"
            )
            raise SemanticError(msg)
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
        # Registration last: a raised violation leaves the checker
        # consistent, so a lenient reader can isolate the block and go on.
        state.last_seq[block.participant_id] = None

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
        # Isolating checks before any state mutation or kind-locking, so a
        # raised violation leaves the checker consistent (block isolation).
        self._note(_prim_finding(block, described))
        self._check_record_order(block, state, described)
        self._classify_record(block, described)
        if block.seq_start is not None:
            state.last_seq[block.sender_pid] = block.seq_start

    def _on_undecoded(self, block: Undecoded) -> None:
        described = _describe(block)
        kind = self._require_source(block.source_id, described)
        if kind != SourceKind.ZPF_INPUT:
            msg = f"{described} must reference a zpf-input source, not {kind!r}"
            raise SemanticError(msg)
        if block.decoder_id is not None and block.decoder_id not in self._decoders:
            msg = f"{described} names undeclared decoder {block.decoder_id}"
            raise SemanticError(msg)
        self._check_reason_class(block, described)
        self._lock_kind(_DECODE, described)

    def _check_reason_class(self, block: Undecoded, described: str) -> None:
        """Check the reason names a recoverability class a consumer can act on.

        The vocabulary is open so a producer can be specific about *how* a
        region came to be undecoded, and ``reason_class`` is what stops that
        freedom costing the consumer the one fact it must have: whether the
        bytes exist upstream. A canonical reason implies its class; anything
        else has to say.
        """
        canonical = UNDECODED_REASONS.get(block.reason or "")
        if block.reason_class is None:
            if block.reason is not None and canonical is None:
                msg = (
                    f"{described} reason {block.reason!r} is outside the canonical "
                    f"vocabulary, so it must carry reason_class"
                )
                raise SemanticError(msg)
            return
        if block.reason_class not in REASON_CLASSES:
            msg = (
                f"{described} reason_class {block.reason_class!r} must be "
                f"'bytes' or 'hole'"
            )
            raise SemanticError(msg)
        if canonical is not None and block.reason_class != canonical:
            msg = (
                f"{described} reason {block.reason!r} is in the {canonical!r} class, "
                f"but reason_class says {block.reason_class!r}"
            )
            raise SemanticError(msg)

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

    def _check_record_order(self, block: Record, state: _SessionState, described: str) -> None:
        """Check (without mutating) that the record respects seq_start order."""
        if block.seq_start is None:
            return
        last = state.last_seq[block.sender_pid]
        if last is not None and not seq_leq(last, block.seq_start):
            msg = (
                f"{described} seq_start {block.seq_start} precedes the participant's "
                f"previous record ({last}); records must be stored in seq_start order"
            )
            raise SemanticError(msg)

    # --- Shared helpers -----------------------------------------------------

    def _note(self, finding: str | None) -> None:
        """Record an advisory finding for the block being observed."""
        if finding is not None:
            self._advisory.append(finding)

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


def _prim_finding(block: Record, described: str) -> str | None:
    """Return the record's advisory ``prim:`` finding, or None if it has none.

    The specification closes the ``prim:`` vocabulary and binds each token's
    width to ``payload_len``, but it also tells a reader meeting an illegal
    token or a width mismatch to treat the label as unknown and keep the
    payload — it "MUST NOT pad, truncate, or reinterpret". So this is a
    writer obligation whose breach costs a reader nothing: reported, never
    isolating. It is the exact complement of
    :func:`~zpf.content.decode_prim` returning None on a ``prim:`` label.
    """
    content_type = block.content_type
    if content_type is None:
        return None
    parsed = ContentType.parse(content_type)
    if not parsed.is_prim:
        return None
    fault = prim_fault(block.payload, parsed.value)
    if fault is None:
        return None
    return f"{described} content_type {content_type!r} {fault}"


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

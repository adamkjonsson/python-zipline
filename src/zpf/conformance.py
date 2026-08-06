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

from zpf._intervals import complement
from zpf.blocks import (
    REASON_CLASSES,
    UNDECODED_REASONS,
    Block,
    Decoder,
    Discontinuity,
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
    described: str = ""
    sequenced: bool = False
    sequenced_basis: str | None = None
    has_hints: bool = False  # any record carried seq_start or ack


@dataclass
class _StreamLedger:
    """What one **input** participant stream was said about, across the file."""

    spans: list[tuple[int, int]] = field(default_factory=list)
    undecoded: list[tuple[int, int]] = field(default_factory=list)
    #: Declared extent per *output* session. More than one distinct value is
    #: a contradiction: an input stream has one length.
    declared: dict[int, int] = field(default_factory=dict)

    @property
    def covered(self) -> list[tuple[int, int]]:
        """Every range this file accounts for, decoded or marked, in order."""
        return sorted(self.spans + self.undecoded)


class CoverageLedger:
    """Accumulates what a derived file says about each input stream it cites.

    The coverage guarantee is a whole-file property: a range is accounted for
    if *any* record's ``spans`` cite it or *any* Undecoded block marks it, and
    the declared extents live on Session End blocks that come last. So no
    single block can be judged as it is read — this gathers the statements and
    :meth:`findings` rules on them once the stream is complete.

    Keyed on the **input** stream ``(source_id, session_id, pid)``, never on
    the output session that happened to cite it. One input stream may be
    demultiplexed into several output sessions (session fan-out), and then no
    single session covers the whole of it — only the union across all of them
    does.
    """

    def __init__(self) -> None:
        self._streams: dict[tuple[int, int, int], _StreamLedger] = {}

    def _for(self, key: tuple[int, int, int]) -> _StreamLedger:
        return self._streams.setdefault(key, _StreamLedger())

    def observe(self, block: Block) -> None:
        """Absorb one block's coverage statements, if it makes any."""
        if isinstance(block, Record):
            for span in block.spans:
                if span.off_start < span.off_end:
                    key = (span.source_id, span.session_id, span.participant_id)
                    self._for(key).spans.append((span.off_start, span.off_end))
        elif isinstance(block, Undecoded):
            if block.off_start < block.off_end:
                key = (block.source_id, block.session_id, block.participant_id)
                self._for(key).undecoded.append((block.off_start, block.off_end))
        elif isinstance(block, SessionEnd):
            for extent in block.input_extents:
                key = (extent.source_id, extent.session_id, extent.participant_id)
                self._for(key).declared[block.session_id] = extent.extent

    def findings(self, file_kind: str | None) -> list[tuple[int, str, str]]:
        """Rule on everything gathered, once the stream is complete.

        Args:
            file_kind: The inferred kind, as
                :attr:`ConformanceChecker.file_kind` reports it. The
                interior-gap check runs only for a decode stage: a
                pass-through re-emits records rather than citing them, so its
                records carry no ``spans`` and its only covered ranges are the
                Undecoded blocks it inherited — everything else would look
                like a hole. Applying a decode stage's obligation to a
                pass-through fails conformant files.

        Returns:
            ``(offset, category, message)`` per finding, in stream order.

        """
        findings: list[tuple[int, str, str]] = []
        for key in sorted(self._streams):
            findings.extend(self._stream_findings(key, self._streams[key], file_kind))
        return findings

    def _stream_findings(
        self, key: tuple[int, int, int], ledger: _StreamLedger, file_kind: str | None
    ) -> list[tuple[int, str, str]]:
        source_id, session_id, pid = key
        where = f"input stream (source {source_id}, session {session_id}, pid {pid})"
        findings: list[tuple[int, str, str]] = []
        covered = ledger.covered
        reach = max((end for _, end in covered), default=0)

        values = set(ledger.declared.values())
        if len(values) > 1:
            spelled = ", ".join(
                f"session {sid} says {ext}" for sid, ext in sorted(ledger.declared.items())
            )
            findings.append((
                0,
                "extents-disagree",
                f"{where}: declared extents disagree ({spelled}); an input stream has one length",
            ))
            # No single declared extent to measure coverage against, so the
            # comparisons below would report a second violation for what is
            # one fault.
            return findings

        if file_kind == _DECODE:
            findings.extend(
                (
                    start,
                    "coverage-gap",
                    f"{where}: [{start}, {end}) is neither decoded nor marked Undecoded",
                )
                for start, end in complement(covered, reach)
            )

        if values:
            extent = next(iter(values))
            if extent > reach:
                findings.append((
                    reach,
                    "extent-exceeds-coverage",
                    f"{where}: declared extent {extent}, but spans and Undecoded blocks "
                    f"account for only [0, {reach}) — the tail is silently dropped",
                ))
            elif extent < reach:
                findings.append((
                    extent,
                    "extent-below-coverage",
                    f"{where}: declared extent {extent}, but spans and Undecoded blocks "
                    f"reach {reach} — the file contradicts itself",
                ))
        return findings


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
        self._derived_only: list[str] = []  # blocks that forbid raw, kind still open
        self._file_ended = False
        self._advisory: list[str] = []  # findings for the block being observed
        self._coverage = CoverageLedger()
        self._dispatch: dict[type[Block], Callable[[Any], None]] = {
            FileHeader: self._on_file_header,
            Source: self._on_source,
            Decoder: self._on_decoder,
            Session: self._on_session,
            Participant: self._on_participant,
            SessionEnd: self._on_session_end,
            Record: self._on_record,
            Undecoded: self._on_undecoded,
            Discontinuity: self._on_discontinuity,
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
        # Only blocks the handlers accepted contribute coverage: an
        # isolated block is one a lenient reader drops, and a dropped
        # block's claims must not count towards the guarantee.
        self._coverage.observe(block)
        # Advisory findings are raised only here, once the handler has
        # returned: a lenient reader keeps such a block, so the checker must
        # already have absorbed it. An isolating violation raised inside the
        # handler skips this, and its notes go with the dropped block.
        if self._advisory:
            raise AdvisoryError("; ".join(self._advisory))

    def check(self, blocks: Iterable[Block]) -> None:
        """Check a whole block sequence (convenience for standalone use).

        Does not finalize: call :meth:`finish` when the stream is complete,
        so a caller may feed a file in several calls.

        Raises:
            AdvisoryError: On the sequence's first advisory finding.
            SemanticError: On the sequence's first isolating violation.

        """
        for block in blocks:
            self.observe(block)

    def finish(self) -> None:
        """Run the checks that only the end of the stream can settle.

        Some obligations cannot be judged when the block carrying them is
        read. Whether a session is *hint-less* is a property of its records,
        and declare-on-first-use puts the Session Descriptor before them — so
        a reader concludes it only at Session End or end-of-stream. Reaching
        the End block or end-of-stream implicitly closes every still-open
        session, and this is that moment.

        The coverage guarantee is the other kind: a range is accounted for if
        *any* record cites it or *any* Undecoded block marks it, and the
        declared extents arrive on Session End blocks at the very end, so no
        single block can be ruled on as it is read. See :class:`CoverageLedger`
        for what that settles and :meth:`coverage_findings` for the whole list.

        Idempotent: finalized sessions are not revisited.

        Raises:
            SemanticError: On the first violation found while finalizing.

        """
        pending = list(self._live.items())
        for session_id, state in pending:
            del self._live[session_id]
            self._ended.add(session_id)
            self._check_sequenced_basis(state)
        for _offset, _category, message in self.coverage_findings():
            raise SemanticError(message)

    def coverage_findings(self) -> list[tuple[int, str, str]]:
        """Return every coverage/extent finding the gathered blocks support.

        :meth:`finish` raises the first of these; this is the whole list, for
        a caller that wants to report rather than refuse (see
        :func:`zpf.check_extents`).

        Returns:
            ``(offset, category, message)`` per finding, in stream order.

        """
        return self._coverage.findings(self._kind)

    def _check_sequenced_basis(self, state: _SessionState) -> None:
        """Require a hint-less sequenced session to say what its order rests on.

        A session carrying `seq`/`ack` derives its order from causal edges
        and needs no basis. One without them has no causal edges at all, so
        the order rests on something the file does not otherwise record —
        which is exactly why the producer must name it. Recording is
        unconditional: ``trivial`` covers the case where there was never a
        cross-participant order to get wrong.
        """
        if not state.sequenced or state.has_hints or state.sequenced_basis is not None:
            return
        msg = (
            f"{state.described} is SEQUENCED and carries no seq/ack on any record, "
            "so it must record what its order rests on in sequenced_basis"
        )
        raise SemanticError(msg)

    # --- Per-block handlers ----------------------------------------------

    def _on_file_header(self, block: FileHeader) -> None:
        if self._header is not None:
            msg = "second File Header; a file has exactly one, as its first block"
            raise SemanticError(msg)
        self._header = block
        if block.transform_params_digest is not None:
            # Names the configuration of a transform that produced records,
            # which a raw capture has not got.
            self._require_derived(f"{_describe(block)} carries transform_params_digest")

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
        self._live[block.session_id] = _SessionState(
            described=_describe(block),
            sequenced=block.sequenced,
            sequenced_basis=block.sequenced_basis,
        )

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
        described = _describe(block)
        if block.input_extents:
            # Measures an input stream, so the file has one.
            self._require_derived(f"{described} carries input_extents")
        state = self._require_live_session(block.session_id, described)
        del self._live[block.session_id]
        self._ended.add(block.session_id)
        self._check_sequenced_basis(state)

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
        if block.seq_start is not None or block.ack is not None:
            state.has_hints = True
        if block.seq_start is not None:
            state.last_seq[block.sender_pid] = block.seq_start

    def _on_discontinuity(self, block: Discontinuity) -> None:
        described = _describe(block)
        state = self._require_live_session(block.session_id, described)
        if block.participant_id not in state.last_seq:
            msg = f"{described} names undeclared participant {block.participant_id}"
            raise SemanticError(msg)
        # Marks a break in a *decoded* stream this file produced, so the file
        # is derived. A raw capture's offsets are true stream positions, in
        # which a hole is already expressible as the space between seq_starts
        # — there is nothing for this block to say and no space for it to say
        # it in.
        self._require_derived(described)

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
        # Not a decode-stage marker any more: a pass-through preserving a
        # decoded layer re-emits its input's Undecoded blocks unchanged,
        # which is what carries the input's coverage guarantee forward. All
        # that follows is that the file is derived.
        self._require_derived(described)

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
        """Lock the file kind implied by this record and check its references.

        The discriminator between the two derived kinds is **spans versus
        origin**, not ``decoder_id``. A record carrying ``spans`` was built
        by this file's stage; one without them was re-emitted from the input
        unchanged. ``decoder_id`` answers a different question — which
        decoder's *layer* the record belongs to — and a pass-through carries
        inherited ones forward, so it says nothing about which stage ran.
        """
        source_kind = self._require_source(block.source_id, described)
        if block.decoder_id is not None:
            if block.decoder_id not in self._decoders:
                msg = f"{described} names undeclared decoder {block.decoder_id}"
                raise SemanticError(msg)
            if source_kind != SourceKind.ZPF_INPUT:
                msg = f"{described} is decoded, so it must reference a zpf-input source"
                raise SemanticError(msg)
        if source_kind == SourceKind.CAPTURE:
            self._lock_kind(_RAW, f"{described} (byte run from a capture source)")
        elif source_kind == SourceKind.ZPF_INPUT:
            if block.spans:
                self._lock_kind(_DECODE, f"{described} (carries spans)")
            else:
                self._lock_kind(_PASS_THROUGH, f"{described} (carries no spans)")
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

    def _require_derived(self, reason: str) -> None:
        """Record that a block rules the file out as raw, whatever kind it is.

        Some blocks say "derived" without saying *which* derived kind, so
        they cannot lock one. Deferring works because the constraint is
        cheap to carry and is settled either way: if a later block locks raw
        the conflict surfaces there, and if it locks a derived kind the
        constraint is already satisfied.
        """
        if self._kind == _RAW:
            msg = (
                f"{reason} implies a derived file, but {self._kind_reason} "
                f"already made it {_RAW} (a file is exactly one kind)"
            )
            raise SemanticError(msg)
        if self._kind is None:
            self._derived_only.append(reason)
            self._require_derived_header(reason)

    def _lock_kind(self, kind: str, reason: str) -> None:
        """Lock the file kind, or verify it matches the already-locked one."""
        if self._kind is None:
            if kind == _RAW and self._derived_only:
                msg = (
                    f"{reason} implies a {_RAW} file, but {self._derived_only[0]} "
                    "already made it derived (a file is exactly one kind)"
                )
                raise SemanticError(msg)
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
            self._derived_only.clear()
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

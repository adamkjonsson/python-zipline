"""Semantic conformance checking for block streams.

The specification's Conformance section states writer obligations that go
beyond well-formed bytes: declare-before-use, id uniqueness, session
lifetime, per-participant record order, and the rules that make each
**stream** well formed as a stream.
:class:`ConformanceChecker` implements them as a single-pass observer over
a block sequence, raising :class:`~zpf.errors.SemanticError` on the first
violation. It is used by the ergonomic writer (always), by the flat
writers when constructed with ``check=True``, and can be run standalone
over any block iterable.

**The unit is the stream, not the file.** Provenance and layer are
independent axes and both are per participant, so one file may hold a
created stream beside a preserved one and a captured stream beside a
derived one. There is no file kind to infer, and asking for one was how
this checker used to reject ``mixed-derivation``. Two rules bind per
participant and are ruled on when its records are all in — at Session End,
or at end-of-stream for a session that never got one:

* its records MUST resolve to **one layer**, or the stream's offset space
  has two incompatible definitions;
* a layer this version does not define MUST NOT be guessed past.

**Provenance is now a per-record rule, and there is one of it:** every
``zpf``-sourced record carries ``spans``. Through ``0.18`` a derived stream
was *created* (records with ``spans``) or *preserved* (a participant with
``origin``), and four rules policed the pair — carry exactly one origin,
never on a capture-sourced stream, never both origin and spans, never
neither. ``0.19`` deleted the option, so a pass-through writes an **identity
span** per record instead, which kind a stream is, is read from whether its
spans are identity, and the four rules collapse into that one sentence. It
binds at the record rather than at Session End, which is both simpler and
earlier: the block a lenient reader isolates is the one that broke it.

Findings come in two strengths. Most are *isolating*: the block cannot be
made sense of, so a lenient reader drops it. A few bind the writer only —
the specification tells a reader that meets them to ignore the offending
label and keep the bytes — and those raise
:class:`~zpf.errors.AdvisoryError`, a :class:`~zpf.errors.SemanticError`
subclass, so writers still refuse the block while a lenient reader can
report it and hand the block over. **Two** rules are advisory today:

* the ``prim:`` content-type ones — an illegal token, or a width that
  disagrees with ``payload_len``. The specification tells a reader to treat
  the label as unknown and keep the payload, so the breach costs it nothing.
  The label grammar and the vocabulary live in :mod:`zpf.content`.
* a ``content_type`` at the **transport** layer. Dropping the label loses
  nothing and the record stays fully readable, so there is no unit a reader
  could soundly discard.

A block with several such findings reports them all in one message.

Reserved flag bits are **not** among them, despite reading like a third: the
format groups a nonzero reserved field with unknown block types and unknown
option ids as part of the extension mechanism, so diagnosing one would report
conformant data as suspect. The comment above ``_ParticipantState`` says so at
the point where a reader of this module would look for the check.

``role`` (``0x0092``) joins the second rule when it is implemented — the
transport-layer bar names both labels in one sentence and gives them one
strength, so the two checks should share this implementation rather than
duplicate it.

Memory stays bounded on unbounded streams: per-session state is freed at
the session's Session End; only the set of ended session ids is retained
(required to police the nothing-after-Session-End rule).

Not checked here, for two different reasons. The decode-stage coverage
guarantee is a whole-file property and is enforced by the transform
helpers. A SEQUENCED session's *interleaving* — that no record follows a
peer record which already acknowledged its bytes — is single-pass and would
fit, but this checker is shared with the read path, where the format lets a
reader trust a sequenced session's stored order rather than re-derive it.
So it is enforced where the promise is made, by
:class:`~zpf.writer.SessionWriter` at write time, and offered on request by
:func:`zpf.order.verify_sequenced`. The per-participant ``seq_start`` half
of that rule *is* checked here, for every session.
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
    OutputLayer,
    Participant,
    Record,
    RecordFlags,
    Session,
    SessionEnd,
    Source,
    SourceKind,
    Undecoded,
)
from zpf.content import ContentType, prim_fault
from zpf.errors import AdvisoryError, SemanticError
from zpf.order import SEQ_SPACE, seq_leq, seq_lt
from zpf.reassembly import layer_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from zpf.blocks import Span

# Reserved flag bits are deliberately *not* checked. The specification puts a
# nonzero reserved field alongside an unknown block type and an unknown option
# id as part of the extension mechanism — "not a violation ... the normal,
# conformant path" — and requires the bit to survive a round-trip without
# being interpreted. Diagnosing one would report conformant data as suspect.

@dataclass
class _ParticipantState:
    """What one **output** stream has said about itself, freed at Session End.

    The unit is the participant, not the file. Provenance and layer are
    independent axes and both are per stream, so one file may hold a
    created stream beside a preserved one, and a captured stream beside a
    derived one — which is what ``mixed-derivation`` ships and what the
    file-purity machinery this replaced could not express.

    Attributes:
        described: The Participant block, for diagnostics.
        isn: Its declared ``isn``, which fixes the stream's origin at
            ``isn + 1``. Kept because two questions need it and neither can
            be answered from a record alone: whether a handshake record sits
            where the format says, and whether a record is placeable at all.
        provenances: The Source kinds its records reference.
        layers: The layers its records resolve to. More than one is a
            violation — the stream's offset space would have two
            incompatible definitions.
        has_discontinuity: The first Discontinuity block naming it, if any.
            Whether that is legal depends on the layer, which is not known
            until the records are in.
        last_seq: Its most recent ``seq_start``, for the stored-order rule.
        prev_reach: Per input stream, the maximum ``off_end`` the previous
            record's spans reached on it — the ``A`` of the unmarked-break
            predicate.
        broke_since: Whether a Discontinuity has appeared since that record,
            which is what satisfies the duty for the pair.
        candidates: Adjacent pairs whose input regions leave a gap and which
            carry no Discontinuity — the only pairs the predicate can fire
            on, held until the holes are all in.

    """

    described: str
    isn: int | None = None
    provenances: set[SourceKind | int] = field(default_factory=set)
    layers: set[OutputLayer | int] = field(default_factory=set)
    has_discontinuity: str | None = None
    last_seq: int | None = None
    prev_reach: dict[tuple[int, int, int], int] = field(default_factory=dict)
    broke_since: bool = False
    candidates: list[_BreakCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class _BreakCandidate:
    """One adjacent pair with an unexplained gap in an input stream.

    Held rather than judged, because whether the gap is a *hole* depends on
    Undecoded blocks that may not have arrived yet: they sit under
    declare-on-first-use alone, so nothing puts them near the records whose
    seam they explain.

    Attributes:
        stream: The input stream ``(source_id, session_id, participant_id)``
            both records cite.
        gap: ``[A, B)`` — one past the first record's reach, up to where the
            second begins.
        described: The second record of the pair, for the message.

    """

    stream: tuple[int, int, int]
    gap: tuple[int, int]
    described: str


@dataclass
class _SessionState:
    """Live per-session bookkeeping, freed at Session End."""

    participants: dict[int, _ParticipantState] = field(default_factory=dict)
    described: str = ""


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
        # Source kinds, so a capture-sourced Undecoded block can be told
        # apart and left out. Declare-on-first-use guarantees the Source
        # arrives before anything referencing it.
        self._sources: dict[int, SourceKind | int] = {}

    def _for(self, key: tuple[int, int, int]) -> _StreamLedger:
        return self._streams.setdefault(key, _StreamLedger())

    def observe(self, block: Block) -> None:
        """Absorb one block's coverage statements, if it makes any."""
        if isinstance(block, Source):
            self._sources[block.source_id] = block.kind
        elif isinstance(block, Record):
            for span in block.spans:
                if span.off_start < span.off_end:
                    key = (span.source_id, span.session_id, span.participant_id)
                    self._for(key).spans.append((span.off_start, span.off_end))
        elif isinstance(block, Undecoded):
            # Against a `capture` source the block discharges no coverage
            # obligation and creates none: the guarantee is scoped within
            # each input participant stream, and a capture has none. Letting
            # it in would invent a stream keyed (source, 0, 0) whose only
            # covered range is this block, and then report everything below
            # it as an unaccounted hole.
            capture = self._sources.get(block.source_id) == SourceKind.CAPTURE
            if block.off_start < block.off_end and not capture:
                key = (block.source_id, block.session_id, block.participant_id)
                self._for(key).undecoded.append((block.off_start, block.off_end))
        elif isinstance(block, SessionEnd):
            for extent in block.input_extents:
                key = (extent.source_id, extent.session_id, extent.participant_id)
                self._for(key).declared[block.session_id] = extent.extent

    def findings(self) -> list[tuple[int, str, str]]:
        """Rule on everything gathered, once the stream is complete.

        The interior-gap check runs for an input stream **some record's
        ``spans`` cited**, which is what makes this file answerable for it.
        That used to be gated on the whole file being a decode stage; the
        per-stream test is both narrower and more accurate, and it survives
        a file that decodes one session while passing another through
        (``mixed-derivation``), which the file-wide gate could not express.

        Since ``0.19`` a pass-through cites **everything**: it writes an
        identity span per record, so it is answerable for its input exactly as
        a decode stage is. Through ``0.18`` it cited nothing and this gate
        existed to keep a decode stage's obligation off it. The gate stays,
        because the question it asks — did any record cite this stream? — is
        still the right one, and a file that declares a ``zpf-input`` Source
        only to resolve an inherited reference cites nothing through it.

        Returns:
            ``(offset, category, message)`` per finding, in stream order.

        """
        findings: list[tuple[int, str, str]] = []
        for key in sorted(self._streams):
            findings.extend(self._stream_findings(key, self._streams[key]))
        return findings

    def _stream_findings(
        self, key: tuple[int, int, int], ledger: _StreamLedger
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

        if ledger.spans:
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
        self._decoders: dict[int, OutputLayer | int] = {}
        self._live: dict[int, _SessionState] = {}
        self._ended: set[int] = set()
        self._holes: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        # Regions the producer said it *removed*, which is the second arm of
        # the seam predicate and the only one that tests a word rather than a
        # class. See _check_unmarked_breaks.
        self._removed: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
        self._breaks: list[_BreakCandidate] = []
        self._transform_digest: str | None = None
        self._saw_zpf_sourced = False
        self._file_ended = False
        self._advisory: list[str] = []  # findings for the block being observed
        # Notes for the block being observed that are **not** findings: a
        # record the offset space cannot place. See `unplaceable_notes`.
        self._unplaceable: list[str] = []
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
        self._unplaceable.clear()
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

    @property
    def unplaceable_notes(self) -> tuple[str, ...]:
        """Records the offset space could not place, from the block just observed.

        **Not findings, and deliberately not raised.** Since `0.19` an
        unplaceable record breaks no rule: the origin floor stopped being a
        MUST NOT and what survives is the effect, which is that such a record
        covers no byte of the stream and contributes nothing to its extent. A
        reader accepts the file and SHOULD *report* the record — so this is a
        third channel beside isolating violations and advisory ones, and it has
        to be, because both of those would make a checking writer refuse a
        block the format permits.

        Read it after :meth:`observe` returns; it is cleared on the next call.
        :class:`~zpf.FileReader` drains it into
        :attr:`~zpf.FileReader.unplaceable`, attaching the file offset it knows
        and the checker does not.

        **One shape is not reported, because a single pass cannot see it.** A
        record with no ``seq_start`` arriving *before* the first record that has
        one, on a participant with no ``isn``, is unplaceable once the later
        hint arrives and anchors the stream — but at the moment it is observed
        nothing says the stream will be anchored at all.
        :func:`zpf.record_ranges` sees the whole participant and does place it
        at zero width; this reports only what is decidable when the block is
        read. Under-reporting is the safe direction: the alternative is calling
        a conformant record unplaceable.

        Returns:
            One note per unplaceable record in the block just observed —
            at most one, a record being a single record.

        """
        return tuple(self._unplaceable)

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
        read. Both axes of a stream are properties of its *records*, and
        declare-on-first-use puts the Participant block before them — so they
        are decidable only once the records are all in. Reaching the End block
        or end-of-stream implicitly closes every still-open session, and this
        is that moment.

        (``0.19`` removed the other reason this phase existed: a hint-less
        SEQUENCED session used to owe a ``sequenced_basis``, which no reader
        could settle before Session End either.)

        The coverage guarantee is the third kind: a range is accounted for if
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
            self._close_participants(state)
        if self._transform_digest is not None and not self._saw_zpf_sourced:
            msg = (
                f"{self._transform_digest} carries transform_params_digest, but every "
                f"stream in this file is capture-sourced; the option is for a stage "
                f"that produced records without decoding, and a reassembler wanting "
                f"its configuration recorded declares itself as a Decoder instead"
            )
            raise SemanticError(msg)
        self._check_unmarked_breaks()
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
        return self._coverage.findings()

    # --- Per-block handlers ----------------------------------------------

    def _on_file_header(self, block: FileHeader) -> None:
        if self._header is not None:
            msg = "second File Header; a file has exactly one, as its first block"
            raise SemanticError(msg)
        self._header = block
        if block.transform_params_digest is not None:
            # "A file all of whose streams are capture-sourced MUST NOT carry
            # the option", and the reason is placement rather than the absence
            # of a transform: reassembly *is* a transform, but a reassembler
            # wanting its configuration recorded declares itself as a Decoder
            # and puts it in that descriptor's params_digest. This option is
            # for a stage that produced records without decoding, which a
            # capture-sourced file has none of. Whole-file, so it settles at
            # finish() rather than here.
            self._transform_digest = _describe(block)

    def _on_source(self, block: Source) -> None:
        if block.source_id in self._sources:
            msg = f"source id {block.source_id} declared twice"
            raise SemanticError(msg)
        self._sources[block.source_id] = block.kind

    def _on_decoder(self, block: Decoder) -> None:
        if block.decoder_id in self._decoders:
            msg = f"decoder id {block.decoder_id} declared twice"
            raise SemanticError(msg)
        self._decoders[block.decoder_id] = block.output_layer

    def _on_session(self, block: Session) -> None:
        if block.session_id in self._live or block.session_id in self._ended:
            msg = f"session id {block.session_id} declared twice"
            raise SemanticError(msg)
        self._live[block.session_id] = _SessionState(
            described=_describe(block),
        )

    def _on_participant(self, block: Participant) -> None:
        described = _describe(block)
        state = self._require_live_session(block.session_id, described)
        if block.participant_id in state.participants:
            msg = (
                f"participant {block.participant_id} of session {block.session_id} "
                "declared twice"
            )
            raise SemanticError(msg)
        # Registration last: a raised violation leaves the checker
        # consistent, so a lenient reader can isolate the block and go on.
        state.participants[block.participant_id] = _ParticipantState(
            described=described, isn=block.isn
        )

    def _on_session_end(self, block: SessionEnd) -> None:
        described = _describe(block)
        if block.input_extents:
            # Measures an input stream, so the file holds one.
            self._require_derived_header(f"{described} carries input_extents")
        state = self._require_live_session(block.session_id, described)
        del self._live[block.session_id]
        self._ended.add(block.session_id)
        self._close_participants(state)

    def _on_record(self, block: Record) -> None:
        described = _describe(block)
        state = self._require_live_session(block.session_id, described)
        stream = state.participants.get(block.sender_pid)
        if stream is None:
            msg = f"{described} names undeclared sender participant {block.sender_pid}"
            raise SemanticError(msg)
        # Isolating checks before any state mutation, so a raised violation
        # leaves the checker consistent (block isolation).
        self._note(_prim_finding(block, described))
        self._note(_handshake_placement(block, stream, described))
        self._check_placement(block, stream, described)
        self._check_record_order(block, stream, described)
        self._classify_record(block, stream, described)
        if block.seq_start is not None:
            stream.last_seq = block.seq_start

    def _on_discontinuity(self, block: Discontinuity) -> None:
        described = _describe(block)
        state = self._require_live_session(block.session_id, described)
        if block.participant_id not in state.participants:
            msg = f"{described} names undeclared participant {block.participant_id}"
            raise SemanticError(msg)
        # A transport-layer stream MUST NOT carry one, whatever its
        # provenance: its offsets are already hole-inclusive, so a gap
        # occupies a real range no payload covers and the two mechanisms
        # would contradict each other. The bar is the layer, which is only
        # known once the participant's records are all in — so this is noted
        # here and ruled on at the stream's close.
        stream = state.participants[block.participant_id]
        stream.has_discontinuity = described
        stream.broke_since = True

    def _on_undecoded(self, block: Undecoded) -> None:
        described = _describe(block)
        kind = self._require_source(block.source_id, described)
        if kind not in (SourceKind.CAPTURE, SourceKind.ZPF_INPUT):
            msg = f"{described} references source {block.source_id} of unknown kind {kind}"
            raise SemanticError(msg)
        if block.decoder_id is not None and block.decoder_id not in self._decoders:
            msg = f"{described} names undeclared decoder {block.decoder_id}"
            raise SemanticError(msg)
        self._check_reason_class(block, described)
        if kind == SourceKind.CAPTURE:
            self._check_against_capture(block, described)
            return
        if block.off_start < block.off_end:
            key = (block.source_id, block.session_id, block.participant_id)
            if _reason_class(block) == "hole":
                self._holes.setdefault(key, []).append((block.off_start, block.off_end))
            elif block.reason == "dropped":
                self._removed.setdefault(key, []).append((block.off_start, block.off_end))
        # Against a `zpf-input` source. Not a decode-stage marker any more:
        # a pass-through preserving a decoded layer re-emits its input's
        # Undecoded blocks unchanged, which is what carries the input's
        # coverage guarantee forward. All that follows is that the file
        # names an input `.zpf`.
        self._require_derived_header(described)

    def _check_against_capture(self, block: Undecoded, described: str) -> None:
        """Check an Undecoded block naming a `capture` Source.

        The body is read by the referenced source's ``kind``, exactly as a
        ``spans`` entry is — one rule for one struct. A capture has no `.zpf`
        inside it, so there is no id namespace to name: the ids are unused
        and the offsets are byte offsets into the capture file.

        The block is legal here because reassembly **is** a transform, and a
        destructive one. What it adds at this position is an overlap the
        reassembler discarded — bytes that are in the capture and did not
        reach the output, which nothing else in the file can express.
        """
        if block.session_id != 0 or block.participant_id != 0:
            msg = (
                f"{described} names a capture source, where there is no id namespace: "
                f"session_id and participant_id are unused and MUST be written 0, "
                f"got {block.session_id} and {block.participant_id}"
            )
            raise SemanticError(msg)
        if _reason_class(block) == "hole":
            msg = (
                f"{described} declares a hole-class region ({block.reason!r}) against a "
                f"capture source, where only the bytes-exist class is available: the "
                f"reassembled stream is a transport layer whose offsets already carry "
                f"the gap, and a second account of the same missing bytes has no rule "
                f"for which to believe"
            )
            raise SemanticError(msg)
        # Nothing else follows. Against a capture source the block discharges
        # no coverage obligation and creates none — the guarantee is scoped
        # within each input participant stream and a capture has none — so it
        # is purely declarative, and the file need not be derived to carry it.

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
        if block.participant_id not in state.participants:
            msg = f"{_describe(block)} names undeclared participant {block.participant_id}"
            raise SemanticError(msg)

    def _on_end(self, block: End) -> None:
        del block
        self._file_ended = True

    # --- Record helpers ----------------------------------------------------

    def _classify_record(
        self, block: Record, stream: _ParticipantState, described: str
    ) -> None:
        """Record what this block says about its stream's two axes.

        Both axes are properties of the *stream*, so they are gathered here
        and decided once all of a participant's records are in, at Session
        End. One thing **is** ruled on here, because it is decidable per
        record: a ``zpf``-sourced record carries ``spans``.

        Created versus preserved is no longer a rule at all, and since
        ``0.19`` it is not a question about which option is present either —
        it is read from **whether the spans are identity**, the same range in
        as out. Nothing here needs to ask: both kinds owe spans, and that is
        the whole of the provenance rule.

        ``decoder_id`` answers a different question — which decoder's *layer*
        a record belongs to — and a pass-through carries inherited ones
        forward, so it says nothing about which stage ran.
        """
        source_kind = self._require_source(block.source_id, described)
        if source_kind not in (SourceKind.CAPTURE, SourceKind.ZPF_INPUT):
            msg = f"{described} references source {block.source_id} of unknown kind {source_kind}"
            raise SemanticError(msg)
        # Carrying a decoder_id no longer implies a zpf-input Source. Both
        # capture-sourced shapes are legal since 0.15 and each has a vector:
        # a decoded stream with no predecessor file (`proxy-decoded`), and a
        # head-of-pipeline reassembler declaring itself with
        # output_layer = transport (`reassembler-declared`).
        if block.decoder_id is None:
            layer: OutputLayer | int = OutputLayer.TRANSPORT
        else:
            declared = self._decoders.get(block.decoder_id)
            if declared is None:
                msg = f"{described} names undeclared decoder {block.decoder_id}"
                raise SemanticError(msg)
            layer = declared
        self._note(_transport_content_type(block, layer, described))
        self._check_spans(block.spans, described=described)
        if source_kind == SourceKind.ZPF_INPUT:
            # **Every `zpf`-sourced record carries `spans`.** One sentence,
            # binding per record, in place of the four participant-level rules
            # `0.19` retired with the `origin` option: every pass-through
            # participant carries exactly one origin; origin MUST NOT appear on
            # a capture-sourced stream; a participant MUST NOT carry both
            # origin and records with spans; a zpf-sourced participant MUST be
            # one or the other. All four keyed on which option was present.
            #
            # It also fires earlier than they could. Those were properties of
            # a participant's whole record set, so they waited for Session End;
            # this is decidable at the record, which is the block a lenient
            # reader isolates.
            if not block.spans:
                msg = (
                    f"{described} references a zpf-input source but carries no spans, "
                    f"so nothing says which stream inside that input its bytes came "
                    f"from; nothing resolves one level down and no coverage obligation "
                    f"can be computed either way"
                )
                raise SemanticError(msg)
            self._require_derived_header(described)
            self._saw_zpf_sourced = True
        stream.provenances.add(source_kind)
        stream.layers.add(layer)
        self._track_break_candidates(block, stream, described)

    def _track_break_candidates(
        self, block: Record, stream: _ParticipantState, described: str
    ) -> None:
        """Note whether this record and its predecessor leave an input gap.

        The single-file half of the origination duty, stated as a predicate
        so that two checkers agree. Each clause below is load-bearing and
        excludes a conformant vector that would otherwise fire:

        * **cited by both** — a unit may span several input streams, and
          fan-out means adjacent units may cite different ones. A stream
          only one of them names says nothing about whether they join
          (``session-fan-out``).
        * **max and min** — ``spans`` may overlap, legal since 0.14.
        * **A ≥ B not tested** — a stage that reorders or overlaps its input
          produces pairs whose input regions run backwards, where "the
          region between them" names nothing (``reordered-decoded``).
        * **a Discontinuity between them** discharges the duty, so the pair
          is not a candidate at all (``filtered-decoded``).

        The layer test is applied later, when the stream closes: it is the
        first clause of the predicate but the last thing knowable.
        """
        reach: dict[tuple[int, int, int], int] = {}
        starts: dict[tuple[int, int, int], int] = {}
        for span in block.spans:
            key = (span.source_id, span.session_id, span.participant_id)
            reach[key] = max(reach.get(key, span.off_end), span.off_end)
            starts[key] = min(starts.get(key, span.off_start), span.off_start)
        if not stream.broke_since:
            for key, begins in starts.items():
                ends = stream.prev_reach.get(key)
                if ends is not None and ends < begins:
                    stream.candidates.append(
                        _BreakCandidate(stream=key, gap=(ends, begins), described=described)
                    )
        stream.prev_reach = reach
        stream.broke_since = False

    def _check_spans(self, spans: tuple[Span, ...], *, described: str) -> None:
        """Check a record's spans name an input this file declared as a `.zpf`.

        Keyed on carrying ``spans`` rather than on carrying a
        ``decoder_id``: a record with spans is a decode stage's output, and
        §Conformance requires those to reference a ``zpf-input`` Source.
        Every ``spans`` entry in all 53 vectors does.

        An **Undecoded** block's body is the same packed shape and is *not*
        checked here, because it may name a capture — that reading is
        Phase 5's.
        """
        for span in spans:
            kind = self._require_source(span.source_id, f"{described} span")
            if kind != SourceKind.ZPF_INPUT:
                msg = f"{described} span must reference a ZPF_INPUT source"
                raise SemanticError(msg)

    def _check_placement(
        self, block: Record, stream: _ParticipantState, described: str
    ) -> None:
        """Note a record the offset space cannot place. Never a violation.

        See :attr:`unplaceable_notes` for why this is a channel of its own and
        which shape it cannot decide.
        """
        anchored = stream.isn is not None or stream.last_seq is not None
        if block.seq_start is None:
            if anchored:
                self._unplaceable.append(
                    f"{described} carries no seq_start on a sequence-anchored stream, "
                    f"so the offset space cannot place it: it covers no byte and "
                    f"contributes nothing to the extent"
                )
            return
        if stream.isn is None:
            return  # no floor to be below; the first hint fixes the origin
        origin = (stream.isn + 1) % SEQ_SPACE
        if seq_lt(block.seq_start, origin):
            self._unplaceable.append(
                f"{described} has seq_start {block.seq_start}, below the stream origin "
                f"{origin} (isn + 1), so the offset space cannot place it: its "
                f"{len(block.payload)} payload byte(s) are excluded from the extent and "
                f"from every coverage answer this file supports"
            )

    def _check_record_order(
        self, block: Record, stream: _ParticipantState, described: str
    ) -> None:
        """Check (without mutating) that the record respects seq_start order."""
        if block.seq_start is None:
            return
        last = stream.last_seq
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

    def _check_unmarked_breaks(self) -> None:
        """Rule on the held pairs, once every Undecoded block is in.

        The predicate has **two arms**, and they are not symmetric.

        A ``hole``-class region between the input regions of two adjacent
        output units admits no other reading: no bytes existed there, so no
        content can have been carried forward, and the two cannot join. That
        arm tests a *class*, so it reaches the whole open vocabulary — ``gap``,
        ``truncated``, and any producer's own hole word carrying
        ``reason_class: hole``.

        The second arm tests a **word**, ``reason = dropped``, because
        content-removed has no class of its own: bytes-exist is the wrong set,
        holding ``skipped`` — which joins — and ``undecodable``, which decides
        nothing. ``0.17`` coined the word so the case became decidable from one
        file at all, and ``0.18`` closed the escape it left by requiring a
        stage that removed content to spell it exactly this way, putting any
        specificity in ``comment``. So the asymmetry is the specification's and
        deliberate. Do **not** widen this arm to bytes-class, which would fire
        on ``undecoded-skipped``, where a discarded byte-order mark withholds
        no content and the text either side runs straight on.

        **Satisfying this is not satisfying the duty.** It is the minimum a
        checker owes, deliberately conservative, and every pair it declines
        to test may still be one where the duty binds — which rests on
        producer knowledge and is mostly not mechanically decidable. A
        producer that emits the block only where this fires has misread it.
        """
        arms = (
            (self._holes, "a hole-class Undecoded region", "no bytes existed in"),
            (self._removed, "an Undecoded region marked dropped", "content was removed from"),
        )
        for candidate in self._breaks:
            start, end = candidate.gap
            for regions, named, because in arms:
                for region_start, region_end in regions.get(candidate.stream, ()):
                    if region_start < end and start < region_end:
                        source_id, session_id, pid = candidate.stream
                        msg = (
                            f"{candidate.described} and the record before it are stored "
                            f"as neighbours, but {named} lies between their input "
                            f"regions on (source {source_id}, session {session_id}, pid "
                            f"{pid}): {because} [{start}, {end}), so the two cannot join "
                            f"and a Discontinuity between them is required"
                        )
                        raise SemanticError(msg)

    def _close_participants(self, state: _SessionState) -> None:
        """Rule on every stream of a session, once its records are all in.

        Deferred to Session End because these are properties of a
        participant's *records*, and declare-on-first-use puts the Participant
        block before them. State is freed here, so live memory stays
        proportional to open sessions.
        """
        for pid in sorted(state.participants):
            self._check_participant(state.participants[pid])

    def _check_participant(self, stream: _ParticipantState) -> None:
        """Check one output stream is well formed as a stream.

        Four rules, each of which the two-axis model made statable and the
        old file-wide classifier could not express.
        """
        if len(stream.layers) > 1:
            named = ", ".join(sorted(layer_name(layer) for layer in stream.layers))
            msg = (
                f"{stream.described} resolves to two layers ({named}), so this "
                f"stream's offset space has no single definition; a reader MUST NOT "
                f"pick one"
            )
            raise SemanticError(msg)
        for layer in stream.layers:
            if not isinstance(layer, OutputLayer):
                msg = (
                    f"{stream.described} references a decoder declaring "
                    f"{layer_name(layer)}, which this version does not define; the "
                    f"stream's offset space cannot be computed and MUST NOT be guessed"
                )
                raise SemanticError(msg)
        if stream.has_discontinuity is not None and OutputLayer.DECODED not in stream.layers:
            msg = (
                f"{stream.has_discontinuity} names a transport-layer stream, which "
                f"MUST NOT carry a Discontinuity: its offsets are hole-inclusive, so "
                f"the break is already expressible as the space no payload covers"
            )
            raise SemanticError(msg)
        # The predicate's first clause: decoded-layer output streams only.
        # A transport stream expresses the same break in its offsets and is
        # forbidden the block, so a checker without this rejects a conformant
        # sessionization stage.
        if OutputLayer.DECODED in stream.layers:
            self._breaks.extend(stream.candidates)

    def _require_derived_header(self, reason: str) -> None:
        header = self._header
        if header is None or header.produced_by is None or header.produced_at is None:
            msg = (
                f"{reason} makes this a derived file, whose File Header must carry "
                "produced_by and produced_at"
            )
            raise SemanticError(msg)


def _transport_content_type(
    block: Record, layer: OutputLayer | int, described: str
) -> str | None:
    """Return the advisory finding for a `content_type` at the transport layer.

    ``content_type`` types a *value* — what this unit **is** — and a
    transport record's boundaries are wherever the reassembler happened to
    chunk the stream. Two conformant reassemblers chunk one stream
    differently and both are right, which is the property the logical offset
    space exists to neutralise; labelling an arbitrary window ``prim:bytes``
    asserts it is a unit when it is a slice. It would also type identical
    bytes differently by provenance, a capture-sourced reassembler declaring
    itself being only a SHOULD.

    **Advisory rather than isolating.** Dropping the label loses nothing and
    the record stays fully readable, so there is no unit a reader could
    soundly discard and nothing it would gain by discarding one — the
    treatment ``tcp_role`` gets. A reader MUST ignore the label and SHOULD
    report it.

    Two statements this docstring used to make are gone, and both were
    already wrong before ``0.19`` removed their basis. It called this the
    *only* MUST NOT with that strength, which stopped being true at ``0.17``
    when ``role`` joined it under one sentence. And it carried the clause
    forbidding a reader to read *layer* from the label, which ``0.19``
    removed as redundant: a reader that ignores a label cannot also draw a
    conclusion from it. Nothing here changes as a result — the layer comes
    from the decoder's ``output_layer`` and never from a label.

    Args:
        block: The record to check.
        layer: The layer its decoder declares, already resolved.
        described: The block, for the message.

    Returns:
        The finding, or ``None`` where there is nothing to report.

    """
    if block.content_type is None or layer is OutputLayer.DECODED:
        return None
    return (
        f"{described} is at the transport layer and MUST NOT carry a content_type "
        f"({block.content_type!r}); the label is ignored and the record kept"
    )


def _handshake_placement(
    block: Record, stream: _ParticipantState, described: str
) -> str | None:
    """Return the advisory finding for a handshake record in the wrong place.

    A writer MAY record an observed TCP handshake as a zero-length record
    carrying the ``syn`` flag, and where it does, the format fixes the shape:
    ``seq_start`` **MUST** be ``isn + 1``. The SYN consumes a sequence number
    without delivering a byte, so the stream origin is one past it, and a
    handshake record written at ``isn`` sits below its own stream — which is
    exactly the file
    `#63 <https://github.com/adamkjonsson/python-zipline/issues/63>`_ was
    filed about.

    **Advisory wherever the record sits**, which `0.18` settled after `0.17`
    left the strengths inverted: a reader accepts the file and SHOULD report.
    What a violation costs is the handshake's timing and nothing else — the
    origin it might otherwise re-derive is fixed by ``isn``, not by this
    record. Reporting it is worth more than isolating it, because a producer
    reading its own output is how the bug gets fixed at the source.

    A ``syn`` record with no ``seq_start`` is not this finding: it is
    unplaceable like any other, which :meth:`ConformanceChecker._check_placement`
    reports.

    Args:
        block: The record to check.
        stream: Its participant's state, for the declared ``isn``.
        described: The block, for the message.

    Returns:
        The finding, or ``None`` where there is nothing to report.

    """
    if not block.flags & RecordFlags.SYN or stream.isn is None:
        return None
    if block.seq_start is None:
        return None
    origin = (stream.isn + 1) % SEQ_SPACE
    if block.seq_start == origin:
        return None
    return (
        f"{described} carries the syn flag with seq_start {block.seq_start}, but a "
        f"handshake record MUST sit at the stream origin isn + 1 ({origin}); the "
        f"record is kept and what is lost is the handshake's timing"
    )


def _reason_class(block: Undecoded) -> str | None:
    """Return an Undecoded block's recoverability class, or None if unknown.

    An explicit ``reason_class`` wins; otherwise a canonical ``reason``
    implies its class. An unrecognised reason with no class is a writer
    error caught separately, and is reported as unknown here rather than
    guessed — in particular never as ``hole``, which would silently discard
    bytes that may well exist.

    Args:
        block: The Undecoded block to classify.

    Returns:
        ``"bytes"``, ``"hole"``, or ``None``.

    """
    if block.reason_class is not None:
        return block.reason_class
    return UNDECODED_REASONS.get(block.reason or "")


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

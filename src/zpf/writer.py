r"""The ergonomic, always-conformant writer: :func:`create` and its handles.

This is the front door for producing ``.zpf`` files. Ids are allocated for
you and passed around as typed handles, descriptors are declared on first
use, and every emitted block flows through a
:class:`~zpf.conformance.ConformanceChecker` — so the easy path writes only
conformant files, and misuse fails at write time with a message naming the
offending blocks.

Example:
    >>> with zpf.create("out.zpf", tick_hz=1_000_000) as w:
    ...     pcap = w.add_source("capture", uri="sideA.pcap")
    ...     with w.begin_session(proto="tcp", key="a <-> b") as s:
    ...         alice = s.participant("10.0.0.1:51000", isn=1000)
    ...         s.record(alice, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n",
    ...                  seq_start=1001, ack=5001)
    ...         s.end(reason="fin")
    ... # End block written on clean exit; skipped if the body raised

The rare Record options without a keyword here (``ts_first``, ``comment``,
``extra_options``) go through the :meth:`FileWriter.write_block` escape
hatch, which is checked exactly like everything else.

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Literal

from zpf.binary import BlockWriter
from zpf.blocks import (
    Block,
    Custom,
    Decoder,
    End,
    FileFlags,
    FileHeader,
    NameResolution,
    Participant,
    Record,
    RecordFlags,
    Session,
    SessionEnd,
    SessionFlags,
    Source,
    SourceKind,
    TcpRole,
    Undecoded,
)
from zpf.conformance import ConformanceChecker
from zpf.errors import ZpfError
from zpf.jsonl import JsonlWriter

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Sequence
    from types import TracebackType
    from typing import Self

    from zpf.blocks import Origin, Span
    from zpf.reader import FileReader

_KIND_NAMES = {"capture": SourceKind.CAPTURE, "zpf-input": SourceKind.ZPF_INPUT}


@dataclass(frozen=True)
class SourceHandle:
    """A declared Source, returned by :meth:`FileWriter.add_source`.

    Attributes:
        source_id: The id records and spans reference.
        kind: The source's :class:`~zpf.blocks.SourceKind`.

    """

    source_id: int
    kind: SourceKind


@dataclass(frozen=True)
class DecoderHandle:
    """A declared Decoder, returned by :meth:`FileWriter.add_decoder`.

    Attributes:
        decoder_id: The id decoded records reference.

    """

    decoder_id: int


@dataclass(frozen=True)
class ParticipantHandle:
    """A declared Participant, returned by :meth:`SessionWriter.participant`.

    Attributes:
        session_id: The session the participant belongs to.
        pid: The participant id within that session.

    """

    session_id: int
    pid: int


@dataclass(frozen=True)
class DerivedInput:
    """The output scaffolding :meth:`FileWriter.derive_from` built from an input.

    Attributes:
        source: The declared ``zpf-input`` Source, to cite input ranges
            against.
        sessions: Output :class:`SessionWriter` per *input* session id.
        participants: Output :class:`ParticipantHandle` per input
            ``(session_id, participant_id)`` pair — the same ids as the
            input, so spans and Undecoded ranges line up.

    """

    source: SourceHandle
    sessions: dict[int, SessionWriter]
    participants: dict[tuple[int, int], ParticipantHandle]

    def handle(self, participant: Participant) -> ParticipantHandle:
        """Return the output handle re-declared for an input participant.

        Args:
            participant: A participant descriptor read from the input.

        Returns:
            The matching output :class:`ParticipantHandle`.

        Raises:
            KeyError: If the participant was not part of the input this
                scaffolding was built from.

        """
        return self.participants[participant.session_id, participant.participant_id]


def _allocate(used: set[int], explicit: int | None, hint: int) -> tuple[int, int]:
    """Pick an id: the explicit one, or the next free counter value.

    Returns:
        The chosen id and the updated counter hint.

    """
    if explicit is not None:
        return explicit, hint
    while hint in used:
        hint += 1
    return hint, hint + 1


class FileWriter:
    """Handle-based writer for one ``.zpf`` file; create with :func:`create`.

    Every block emitted through this writer — including the
    :meth:`write_block` escape hatch — is validated by a
    :class:`~zpf.conformance.ConformanceChecker`, so a violation raises
    :class:`~zpf.errors.SemanticError` before anything hits the sink.

    As a context manager, a clean exit writes the End block (marking the
    file complete); an exception skips it, leaving the file honestly
    incomplete. Sessions never explicitly ended are closed implicitly by
    the End block, exactly as the specification allows.
    """

    def __init__(
        self,
        sink: str | os.PathLike[str] | IO[bytes] | IO[str],
        *,
        tick_hz: int,
        time_epoch: int | None = None,
        creator: str | None = None,
        produced_by: str | None = None,
        produced_at: int | None = None,
        single_clock: bool = False,
        comment: str | None = None,
        face: Literal["binary", "jsonl"] = "binary",
    ) -> None:
        if face == "binary":
            self._writer: BlockWriter | JsonlWriter = BlockWriter(sink)
        elif face == "jsonl":
            self._writer = JsonlWriter(sink)
        else:
            msg = f"face must be 'binary' or 'jsonl', got {face!r}"
            raise ValueError(msg)
        self._checker = ConformanceChecker()
        self._sources: dict[int, SourceHandle] = {}
        self._decoder_ids: set[int] = set()
        self._session_ids: set[int] = set()
        self._next_source = 0
        self._next_decoder = 0
        self._next_session = 0
        self._closed = False
        self._ended = False
        flags = FileFlags.SINGLE_CLOCK if single_clock else FileFlags(0)
        self._header = FileHeader(
            tick_hz=tick_hz,
            time_epoch=time_epoch,
            creator=creator,
            produced_by=produced_by,
            produced_at=produced_at,
            flags=flags,
            comment=comment,
        )
        self._emit(self._header)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close(end=exc_type is None)

    @property
    def closed(self) -> bool:
        """Whether the writer has been closed."""
        return self._closed

    @property
    def header(self) -> FileHeader:
        """The File Header written when this file was created."""
        return self._header

    def add_source(
        self,
        kind: SourceKind | str,
        *,
        uri: str | None = None,
        digest: str | None = None,
        link_type: int | None = None,
        comment: str | None = None,
        source_id: int | None = None,
    ) -> SourceHandle:
        """Declare a Source and return its handle.

        Args:
            kind: ``"capture"``, ``"zpf-input"``, or a
                :class:`~zpf.blocks.SourceKind`.
            uri: Where the referenced capture/input file lives.
            digest: Content hash of the referenced file (``"<alg>:<hex>"``).
            link_type: Link-layer type (capture sources only).
            comment: Free-text note.
            source_id: Explicit id for producers with their own id scheme;
                allocated automatically when omitted.

        Returns:
            The handle records, spans, and origins reference.

        """
        if isinstance(kind, str):
            if kind not in _KIND_NAMES:
                msg = f"kind must be 'capture' or 'zpf-input', got {kind!r}"
                raise ValueError(msg)
            kind = _KIND_NAMES[kind]
        chosen, self._next_source = _allocate(set(self._sources), source_id, self._next_source)
        self._emit(
            Source(
                source_id=chosen, kind=kind, uri=uri, digest=digest,
                link_type=link_type, comment=comment,
            )
        )
        handle = SourceHandle(source_id=chosen, kind=SourceKind(kind))
        self._sources[chosen] = handle
        return handle

    def add_decoder(
        self,
        name: str,
        *,
        version: str | None = None,
        params_digest: str | None = None,
        comment: str | None = None,
        decoder_id: int | None = None,
    ) -> DecoderHandle:
        """Declare a Decoder (decode-stage files) and return its handle.

        Args:
            name: Decoder identifier, e.g. ``"http/1.1"``.
            version: Decoder version.
            params_digest: Hash of the decoder config.
            comment: Free-text note.
            decoder_id: Explicit id; allocated automatically when omitted.

        Returns:
            The handle decoded records reference.

        """
        chosen, self._next_decoder = _allocate(self._decoder_ids, decoder_id, self._next_decoder)
        self._emit(
            Decoder(
                decoder_id=chosen, name=name, version=version,
                params_digest=params_digest, comment=comment,
            )
        )
        self._decoder_ids.add(chosen)
        return DecoderHandle(decoder_id=chosen)

    def begin_session(
        self,
        *,
        proto: str | None = None,
        key: str | None = None,
        sequenced: bool = False,
        comment: str | None = None,
        session_id: int | None = None,
    ) -> SessionWriter:
        """Declare a Session and return its writer.

        Args:
            proto: Session protocol (lowercase; e.g. ``"tcp"``).
            key: Human-readable flow key.
            sequenced: Set the SEQUENCED flag — the caller asserts it will
                emit this session's records in a valid causal order.
            comment: Free-text note.
            session_id: Explicit id (e.g. from a global monotonic
                sequence); allocated automatically when omitted.

        Returns:
            A :class:`SessionWriter`; also usable as a context manager
            that emits the Session End on clean exit.

        """
        chosen, self._next_session = _allocate(self._session_ids, session_id, self._next_session)
        flags = SessionFlags.SEQUENCED if sequenced else SessionFlags(0)
        self._emit(
            Session(session_id=chosen, proto=proto, flow_key=key, flags=flags, comment=comment)
        )
        self._session_ids.add(chosen)
        return SessionWriter(self._emit, self._default_source, chosen)

    def derive_from(
        self,
        reader: FileReader,
        *,
        uri: str | None = None,
        digest: str | None = None,
        proto: str | None = None,
        comment: str | None = None,
    ) -> DerivedInput:
        """Scaffold this file as one derived from ``reader``.

        Declares the ``zpf-input`` Source (hashing the input when no
        ``digest`` is given), then mirrors the input's sessions and
        participants onto this writer, re-declaring every participant under
        the *same* ids so spans and Undecoded ranges need no translation.

        The File Header is written when the file is created, so this method
        cannot copy ``tick_hz``/``time_epoch`` onto it — it checks they
        already agree and raises if they do not. Use :func:`zpf.decode_stage`
        to have the header set for you.

        ``isn`` is deliberately not copied: it describes the input's raw TCP
        stream, which the derived records are no longer in. Sessions are not
        marked sequenced, since a decoder that walks one stream at a time
        does not emit a causal order across them.

        Args:
            reader: The open input file.
            uri: Where the input lives; defaults to the path it was opened
                from.
            digest: Content hash of the input; computed with SHA-256 when
                omitted.
            proto: Protocol for the output sessions (e.g. ``"http"``);
                defaults to the input's.
            comment: Free-text note for the Source.

        Returns:
            The :class:`DerivedInput` scaffolding: source handle, session
            writers, and the participant map.

        Raises:
            ZpfError: If the input has no File Header, or its time base
                disagrees with this file's.

        """
        header = reader.header
        if header is None:
            msg = "input file has no File Header to derive from"
            raise ZpfError(msg)
        self._check_time_base(header)
        source = self.add_source(
            SourceKind.ZPF_INPUT,
            uri=uri if uri is not None else reader.path,
            digest=digest if digest is not None else reader.digest(),
            comment=comment,
        )
        sessions: dict[int, SessionWriter] = {}
        participants: dict[tuple[int, int], ParticipantHandle] = {}
        for session in reader.sessions():
            out = self.begin_session(
                proto=proto if proto is not None else session.proto,
                key=session.key,
                session_id=session.session_id,
            )
            sessions[session.session_id] = out
            for participant in session.participants:
                participants[session.session_id, participant.participant_id] = out.participant(
                    participant.endpoints or None,
                    tcp_role=participant.tcp_role,
                    identity=participant.identity,
                    pid=participant.participant_id,
                )
        return DerivedInput(source=source, sessions=sessions, participants=participants)

    def _check_time_base(self, header: FileHeader) -> None:
        """Reject an input whose timestamps would not mean the same thing here."""
        mine = self._header
        if header.tick_hz != mine.tick_hz:
            msg = (
                f"input tick_hz {header.tick_hz} differs from this file's "
                f"{mine.tick_hz}; create the output with tick_hz={header.tick_hz} "
                "(or use zpf.decode_stage, which copies it for you)"
            )
            raise ZpfError(msg)
        if header.time_epoch != mine.time_epoch:
            msg = (
                f"input time_epoch {header.time_epoch} differs from this file's "
                f"{mine.time_epoch}; create the output with "
                f"time_epoch={header.time_epoch} (or use zpf.decode_stage)"
            )
            raise ZpfError(msg)

    def undecoded(
        self,
        source: SourceHandle,
        session_id: int,
        pid: int,
        off_start: int,
        off_end: int,
        *,
        reason: str | None = None,
        decoder: DecoderHandle | None = None,
        comment: str | None = None,
    ) -> None:
        """Mark an input region this decode stage did not decode.

        ``session_id`` and ``pid`` are read in the *input's* id namespace
        (they name a stream inside ``source``), so they are plain ints
        here, not handles.

        Args:
            source: The ``zpf-input`` Source whose stream the offsets index.
            session_id: Session inside that input.
            pid: Participant (stream) inside that input.
            off_start: First logical offset of the region.
            off_end: One past the region's last offset.
            reason: ``"undecodable"``, ``"tcp-gap"``, ``"truncated"``, …
            decoder: Which decoder declined the region.
            comment: Free-text note.

        """
        self._emit(
            Undecoded(
                source_id=source.source_id,
                session_id=session_id,
                participant_id=pid,
                off_start=off_start,
                off_end=off_end,
                reason=reason,
                decoder_id=None if decoder is None else decoder.decoder_id,
                comment=comment,
            )
        )

    def write_custom(self, pen: int, subtype: int, payload: bytes) -> None:
        """Write a vendor Custom block (payload length a multiple of 4)."""
        self._emit(Custom(pen=pen, subtype=subtype, payload=payload))

    def write_block(self, block: Block) -> None:
        """Write any pre-built block — the escape hatch for exotic options.

        The block passes through the same conformance checking as blocks
        built by the handle methods.
        """
        self._emit(block)

    def close(self, *, end: bool = True) -> None:
        """Finish the file.

        Args:
            end: Write the End block (marks the file complete). The
                context manager passes ``end=False`` when the body raised,
                so a failed write is honestly incomplete.

        """
        if self._closed:
            return
        if end and not self._ended:
            self._emit(End())
        self._closed = True
        self._writer.close()

    # --- internal ----------------------------------------------------------

    def _emit(self, block: Block) -> None:
        """Check, then write: a violation raises before bytes are produced."""
        if self._closed:
            msg = "writer is closed"
            raise ZpfError(msg)
        self._checker.observe(block)
        self._writer.write(block)
        if isinstance(block, End):
            self._ended = True

    def _default_source(self) -> SourceHandle:
        if len(self._sources) != 1:
            msg = (
                f"the file declares {len(self._sources)} sources; "
                "pass source= to say which one this record came from"
            )
            raise ZpfError(msg)
        return next(iter(self._sources.values()))


class SessionWriter:
    """Writer for one session's participants, records, and end marker.

    Obtained from :meth:`FileWriter.begin_session`. As a context manager,
    a clean exit calls :meth:`end` automatically (with no reason);
    call :meth:`end` yourself to say how the session actually ended.

    Attributes:
        session_id: The session's id in the file.

    """

    def __init__(
        self,
        emit: Callable[[Block], None],
        default_source: Callable[[], SourceHandle],
        session_id: int,
    ) -> None:
        self._emit = emit
        self._default_source = default_source
        self.session_id = session_id
        self._pids: set[int] = set()
        self._next_pid = 0
        self._ended = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None and not self._ended:
            self.end()

    def participant(
        self,
        endpoint: str | Sequence[str] | None = None,
        *,
        isn: int | None = None,
        tcp_role: TcpRole | None = None,
        identity: str | None = None,
        origin: Origin | None = None,
        comment: str | None = None,
        pid: int | None = None,
    ) -> ParticipantHandle:
        """Declare a participant of this session and return its handle.

        Args:
            endpoint: One address, or an ordered sequence for tunnelled
                participants (outermost carrier first, innermost last).
            isn: The SYN's sequence number; must be given when the
                handshake was observed.
            tcp_role: Which side opened the connection, when known.
            identity: Stable identity distinct from a transient endpoint.
            origin: Input-stream mapping (pass-through files only).
            comment: Free-text note.
            pid: Explicit participant id; allocated automatically when
                omitted.

        Returns:
            The handle used as a record's ``sender``.

        """
        endpoints = (endpoint,) if isinstance(endpoint, str) else tuple(endpoint or ())
        chosen, self._next_pid = _allocate(self._pids, pid, self._next_pid)
        self._emit(
            Participant(
                session_id=self.session_id,
                participant_id=chosen,
                endpoints=endpoints,
                isn=isn,
                tcp_role=tcp_role,
                identity=identity,
                origin=origin,
                comment=comment,
            )
        )
        self._pids.add(chosen)
        return ParticipantHandle(session_id=self.session_id, pid=chosen)

    def record(
        self,
        sender: ParticipantHandle,
        ts: int,
        payload: bytes = b"",
        *,
        source: SourceHandle | None = None,
        seq_start: int | None = None,
        ack: int | None = None,
        flags: RecordFlags | int = 0,
        decoder: DecoderHandle | None = None,
        content_type: str | None = None,
        spans: tuple[Span, ...] = (),
    ) -> None:
        """Write one record of this session.

        Args:
            sender: The participant that sent these bytes.
            ts: Packet time in ``tick_hz`` ticks (completion time for
                reassembled payloads).
            payload: The payload bytes (empty for a pure-ACK record).
            source: Which declared Source the bytes came from; may be
                omitted when the file declares exactly one.
            seq_start: Absolute TCP sequence number of the first byte.
            ack: The acknowledgement number from the wire.
            flags: Record flags.
            decoder: The decoder that produced this record (decoded
                records only; its presence is what makes a record decoded).
            content_type: ``mime:``/``prim:``/``dec:`` payload label.
            spans: Source ranges the bytes were built from.

        Rare options without a keyword here (``ts_first``, ``comment``,
        ``extra_options``) go through :meth:`FileWriter.write_block`.

        """
        if sender.session_id != self.session_id:
            msg = (
                f"sender belongs to session {sender.session_id}, "
                f"not this session ({self.session_id})"
            )
            raise ZpfError(msg)
        resolved = source if source is not None else self._default_source()
        self._emit(
            Record(
                session_id=self.session_id,
                sender_pid=sender.pid,
                source_id=resolved.source_id,
                timestamp=ts,
                payload=payload,
                flags=flags,
                seq_start=seq_start,
                ack=ack,
                spans=spans,
                decoder_id=None if decoder is None else decoder.decoder_id,
                content_type=content_type,
            )
        )

    def name(
        self,
        participant: ParticipantHandle,
        label: str,
        *,
        kind: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Attach a human label to a participant after the fact.

        Args:
            participant: The participant being labelled.
            label: The human-readable name.
            kind: Source of the label (``"nick"``, ``"dns"``, ``"tls-sni"``).
            comment: Free-text note.

        """
        self._emit(
            NameResolution(
                session_id=self.session_id,
                participant_id=participant.pid,
                label=label,
                kind=kind,
                comment=comment,
            )
        )

    def end(self, reason: str | None = None, *, comment: str | None = None) -> None:
        """Declare the file holds nothing more for this session.

        Args:
            reason: How the session ended: ``"fin"``, ``"rst"``,
                ``"timeout"``, ``"capture-end"``, … (open vocabulary).
            comment: Free-text note.

        """
        self._emit(SessionEnd(session_id=self.session_id, reason=reason, comment=comment))
        self._ended = True


def create(
    sink: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    tick_hz: int,
    time_epoch: int | None = None,
    creator: str | None = None,
    produced_by: str | None = None,
    produced_at: int | None = None,
    single_clock: bool = False,
    comment: str | None = None,
    face: Literal["binary", "jsonl"] = "binary",
) -> FileWriter:
    """Create a ``.zpf`` file and return its handle-based writer.

    The File Header is written immediately. Derived files (decode-stage or
    pass-through) must set ``produced_by`` and ``produced_at`` here — the
    conformance checker enforces it the moment the file's kind becomes
    derived.

    Args:
        sink: Output path or stream (binary for the binary face, text for
            the JSONL face).
        tick_hz: Time units per second for all timestamps (non-zero).
        time_epoch: Timestamp origin in ticks since the Unix epoch.
        creator: Tool + version writing the file.
        produced_by: Tool + version of the transform (derived files).
        produced_at: Wall-clock build time in Unix seconds (derived files).
        single_clock: Assert every record is stamped against one
            trustworthy clock (the SINGLE_CLOCK file flag).
        comment: Free-text note.
        face: ``"binary"`` (the canonical container) or ``"jsonl"``.

    Returns:
        An open :class:`FileWriter` (also a context manager: a clean exit
        writes the End block, an exception skips it).

    """
    return FileWriter(
        sink,
        tick_hz=tick_hz,
        time_epoch=time_epoch,
        creator=creator,
        produced_by=produced_by,
        produced_at=produced_at,
        single_clock=single_clock,
        comment=comment,
        face=face,
    )

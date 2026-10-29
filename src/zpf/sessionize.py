"""The sessionization stage — a reassembler run over a ``.zpf``, fanning out.

A **sessionization stage** reads a ``.zpf`` of packets and writes the
conversations inside them: a reassembler whose Decoder declares
``output_layer = transport``, run over a file rather than a capture. It is the
hop that lets a decrypted tunnel's packets become TCP sessions without
leaving ``.zpf`` for a pcap, which would sever provenance (the
specification's *Worked example: a decrypted tunnel*).

It differs from a decode stage in one way that changes everything else: it
**fans out**. One input stream — the tunnel — becomes as many output sessions
as there are flows inside it, each with fresh ids, its own ``isn`` and its
own sequence numbers, and each record citing the input stream it came from.
So :func:`zpf.decode_stage`, which mirrors its input's sessions one to one
under the same ids, cannot express it, and :func:`sessionize_stage` exists::

    with zpf.sessionize_stage(
        "packets.zpf", "inner.zpf",
        decoder=("tcp-reassembly", "1.1"),
        produced_by="my-sessionizer 1.0",
        produced_at=1_700_000_000,
    ) as stage:
        for stream in stage.streams():
            for dg in stream.datagrams():           # one inner packet each
                flow = classify(dg.data)            # your packet parsing
                session = stage.session(flow.key, proto="tcp")
                sender = session.participant(flow.src, isn=flow.isn)
                session.record(
                    sender, flow.payload,
                    cites=stream.cite(dg.off_start, dg.off_end),
                    hints=zpf.Hints(seq_start=flow.seq, ack=flow.ack),
                )

What the stage does for you, and :func:`zpf.decode_stage` does too:

* **Coverage**, per *input* stream across *every* output session. Each
  citation is counted against the input stream it names, whichever session
  it was written for, and on a clean close auto-fill marks the rest
  ``skipped`` (or ``gap``) — the guarantee the specification states per
  input participant stream, which under fan-out no single session meets.
* **Extents.** Each output session's Session End declares the **whole**
  extent of every input stream it cited — *under fan-out a consuming session
  declares the whole input stream, not its share.*
* **Timestamps**, derived from what a record cites.

What it does **not** do is parse packets. The format defines what a
``net:ip`` payload is, not its fields, so classifying a packet into a flow is
the caller's code, and the stage takes the caller's word for the sequence
numbers. They are the stage's assertion, and so is the next one. A transport
record that carries the **evidence of its own join** MAY cite several input
units, or cross a break its input declares. There are two kinds of evidence:

* **sequence numbers**, a record carrying ``seq_start``, since ``0.22``
  (`zipline#152 <https://github.com/adamkjonsson/zipline/issues/152>`_). They
  say the bytes join, and a break between them would be a range no record
  covers, which cannot be lost;
* **one message's reassembly**, a record flagged
  ``RecordFlags.MESSAGE``, since ``0.24``
  (`zipline#166 <https://github.com/adamkjonsson/zipline/issues/166>`_). The
  record is exactly one transport message, so the units it cites are that
  message's fragments: the IP fragments of one datagram, or the SCTP DATA
  chunks of one message.

A record carrying neither may cite one range only. A datagram that lost a
fragment is no message at all, and gets no record: mark its pieces with
:meth:`SessionizeStage.incomplete`, which writes the ``undecodable`` blocks
the specification asks for (``0.24``, `zipline#170
<https://github.com/adamkjonsson/zipline/issues/170>`_).

A **pure ACK** is a zero-length record citing its packet, with its ``ack``:
the packet owes coverage, and the ack orders it. Since ``0.23``
(`zipline#160 <https://github.com/adamkjonsson/zipline/issues/160>`_) it
occupies no sequence number, so it orders nothing after it — which is what
lets a flow with stored pure ACKs be merged and verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Literal

from zpf.blocks import OutputLayer, RecordFlags, SourceKind, Span
from zpf.decode import (
    InputStream,
    _declare_decoder,
    _derive_ts,
    _derive_ts_first,
    _Ledger,
    _open_input,
)
from zpf.errors import SemanticError, ZpfError
from zpf.writer import Decoded, InputRef, create

if TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime
    from types import TracebackType
    from typing import Self

    from zpf.blocks import InputExtent, TcpRole
    from zpf.reader import FileReader
    from zpf.writer import (
        DecoderHandle,
        FileWriter,
        Hints,
        ParticipantHandle,
        SessionWriter,
        SourceHandle,
    )


@dataclass(frozen=True)
class _StageContext:
    """What a :class:`FlowSession` needs from its stage, and nothing else."""

    source: SourceHandle
    decoder: DecoderHandle
    inputs: Mapping[tuple[int, int], InputStream]
    ledger: _Ledger


class FlowSession:
    """One output session of a sessionization stage: one flow.

    Created on demand by :meth:`SessionizeStage.session`, which returns the
    same object for the same key. Its participants are created on demand too,
    one per endpoint.

    Attributes:
        key: The flow key the session was created for, written as the
            session's ``key``.
        proto: The session's protocol.
        writer: The output session's writer.

    """

    def __init__(
        self, stage: _StageContext, writer: SessionWriter, key: str, proto: str
    ) -> None:
        self._stage = stage
        self.key = key
        self.proto = proto
        self.writer = writer
        self._participants: dict[tuple[str, ...], tuple[ParticipantHandle, int | None]] = {}
        self._cited: dict[tuple[int, int], InputStream] = {}
        self._ended = False

    @property
    def session_id(self) -> int:
        """The output session's id."""
        return self.writer.session_id

    def participant(
        self,
        endpoint: str | Sequence[str],
        *,
        isn: int | None = None,
        tcp_role: TcpRole | None = None,
        identity: str | None = None,
    ) -> ParticipantHandle:
        """Return the participant for ``endpoint``, declaring it on first use.

        Args:
            endpoint: One address, or an ordered sequence for a tunnelled
                one. The cache key.
            isn: The SYN's sequence number, when the handshake was seen.
                Fixes the stream's origin at ``isn + 1``.
            tcp_role: Initiator or responder, where known.
            identity: A stable identity for the endpoint.

        Returns:
            The participant's handle.

        Raises:
            ValueError: If the endpoint was declared already with a
                different ``isn``. A participant's origin cannot move.

        """
        cache_key = (endpoint,) if isinstance(endpoint, str) else tuple(endpoint)
        known = self._participants.get(cache_key)
        if known is not None:
            handle, declared_isn = known
            if isn is not None and isn != declared_isn:
                msg = (
                    f"participant {cache_key!r} of session {self.key!r} was declared with "
                    f"isn {declared_isn}, not {isn}; a participant's origin cannot move"
                )
                raise ValueError(msg)
            return handle
        handle = self.writer.participant(
            list(cache_key), isn=isn, tcp_role=tcp_role, identity=identity
        )
        self._participants[cache_key] = (handle, isn)
        return handle

    def record(
        self,
        participant: ParticipantHandle,
        payload: bytes = b"",
        *,
        cites: Span | Iterable[Span],
        ts: int | None = None,
        ts_first: int | None = None,
        hints: Hints | None = None,
        flags: RecordFlags | int = 0,
        comment: str | None = None,
    ) -> None:
        """Write one transport record for ``participant``, citing the input.

        Args:
            participant: A participant of this session, from
                :meth:`participant`.
            payload: The record's bytes.
            cites: The input ranges the record was built from, as
                :class:`~zpf.blocks.Span` objects — build them with
                :meth:`InputStream.cite <zpf.decode.InputStream.cite>`, which
                names the input stream as well as the range. A record citing
                nothing is refused: a stage's record says where it came from.
            ts: Record time in the file's ticks. **Omit it and it is derived
                from** ``cites``: the completion time of the last input record
                the record draws on, as for a decode stage.
            ts_first: Time of the **first** input packet the record draws on,
                where ``ts`` is the last — a reassembler's field, for a record
                coalesced from several packets
                (`#86 <https://github.com/adamkjonsson/python-zipline/issues/86>`_).
                **Derived along with** ``ts`` when both are omitted: the
                earliest arrival among the cited input records, each counting
                its own ``ts_first`` where it has one. It is written only when
                it is earlier than ``ts``, so a record built from one packet
                carries none, as the specification advises. Given explicitly,
                it is written as given.
            hints: ``seq_start`` and ``ack`` from the flow. With ``seq_start``
                the record MAY cite several input units, or cross a break the
                input declares (`zipline#152
                <https://github.com/adamkjonsson/zipline/issues/152>`_).
            flags: Record flags. With
                ``RecordFlags.MESSAGE`` the record is exactly
                one transport message, and MAY cite several input units, or
                cross a declared break, as that message's fragments: a UDP
                datagram reassembled from IP fragments, say (`zipline#166
                <https://github.com/adamkjonsson/zipline/issues/166>`_).
            comment: Free-text note on the record.

        Raises:
            SemanticError: If the record cites nothing, or cites more than
                one range with neither ``seq_start`` nor ``MESSAGE``.
            ZpfError: If the session has ended.

        """
        spans = (cites,) if isinstance(cites, Span) else tuple(cites)
        _check_cites(spans, hints, flags)
        if self._ended:
            msg = f"session {self.key!r} has ended; no record can be added to it"
            raise ZpfError(msg)
        if ts is None:
            ts = _derive_ts(self._stage.source, self._stage.inputs, spans)
            if ts_first is None:
                first = _derive_ts_first(self._stage.source, self._stage.inputs, spans)
                ts_first = first if first is not None and first < ts else None
        self.writer.record(
            participant,
            ts=ts,
            ts_first=ts_first,
            payload=payload,
            flags=flags,
            hints=hints,
            decoded=Decoded(decoder=self._stage.decoder, spans=spans),
            comment=comment,
        )
        # After the write, as everywhere a stage counts coverage.
        self._stage.ledger.cite(spans)
        for span in spans:
            stream = self._stage.inputs.get((span.session_id, span.participant_id))
            if stream is not None:
                self._cited[span.session_id, span.participant_id] = stream

    def end(self, reason: str | None = None, *, comment: str | None = None) -> None:
        """End the session now, declaring the extent of every input stream it cited.

        A session left open is ended when the stage closes, with no reason.
        End it yourself to say how it ended (``"fin"``, ``"rst"``, …) or to
        stop holding it open.

        Args:
            reason: How the session ended (open vocabulary).
            comment: Free-text note.

        """
        if self._ended:
            return
        self._ended = True
        self.writer.end(reason, input_extents=self.input_extents(), comment=comment)

    def input_extents(self) -> list[InputExtent]:
        """Return the whole extent of each input stream this session has cited."""
        return [self._stage.ledger.extent(stream) for stream in self._cited.values()]

    @property
    def ended(self) -> bool:
        """Whether the session's Session End has been written."""
        return self._ended


class SessionizeStage:
    """An open sessionization stage: packets being read, flows being written.

    Created by :func:`sessionize_stage`, normally as a context manager. A
    clean exit auto-fills the input's uncovered ranges, ends every session
    still open, and closes the output and then the input, if this stage
    opened it.

    Attributes:
        reader: The input file.
        writer: The output file.
        source: The ``zpf-input`` Source the output cites.
        decoder: The stage's Decoder, declared at the transport layer.

    """

    def __init__(
        self,
        reader: FileReader,
        writer: FileWriter,
        source: SourceHandle,
        decoder: DecoderHandle,
        *,
        owns_reader: bool,
        fill_undecoded: bool = True,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.source = source
        self.decoder = decoder
        self._ledger = _Ledger(writer, source, decoder)
        self._owns_reader = owns_reader
        self._fill_undecoded = fill_undecoded
        self._inputs: dict[tuple[int, int], InputStream] = {
            (view.participant.session_id, view.participant.participant_id): InputStream(
                view.cited_as(source)
            )
            for session in reader.sessions()
            for view in session.reassemble()
        }
        self._context = _StageContext(source, decoder, self._inputs, self._ledger)
        # The key's current session, and every session in creation order: a
        # key gets a new session once its last one has ended (#88).
        self._current: dict[str, FlowSession] = {}
        self._sessions: list[FlowSession] = []
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close(end=exc_type is None)

    def streams(self) -> tuple[InputStream, ...]:
        """Return every input stream, in the input's declaration order."""
        return tuple(self._inputs.values())

    def session(
        self,
        key: str,
        *,
        proto: str,
        session_id: int | None = None,
        sequenced: bool = False,
    ) -> FlowSession:
        """Return the open session for flow ``key``, creating one if there is none.

        **The end of a session is not the end of its key**
        (`#88 <https://github.com/adamkjonsson/python-zipline/issues/88>`_).
        Once the key's session has ended, the next call opens a new session on
        the same key. That is what a reassembler does at a hole of 2³¹ bytes
        or more — the specification's ``capture-gap`` rule is to end the
        session at the hole and *open another on the same key* — and after an
        idle ``timeout`` or a port reused after ``fin``/``rst``. A consumer
        tells the sessions apart by their Session End reasons, which is what
        the rule asks the same key for.

        Args:
            key: The flow key, e.g. ``"10.8.0.2:44300 -> 10.8.0.9:80"``.
                Written as the session's ``key``, and the cache key.
            proto: The session's protocol, e.g. ``"tcp"``.
            session_id: An explicit id for a new session; fresh by default.
            sequenced: Store the session's records in causal order and mark
                it SEQUENCED. They are buffered until the session ends.

        Returns:
            The flow's open session.

        Raises:
            ValueError: If ``key`` has an open session with a different
                ``proto``, or ``session_id`` names a different id from the
                one the key's open session has.

        """
        known = self._current.get(key)
        if known is not None and not known.ended:
            if known.proto != proto:
                msg = (
                    f"session {key!r} was created with proto {known.proto!r}, "
                    f"not {proto!r}"
                )
                raise ValueError(msg)
            if session_id is not None and session_id != known.session_id:
                msg = (
                    f"session {key!r} is open as session {known.session_id}, so "
                    f"session_id {session_id} cannot open another; end it first"
                )
                raise ValueError(msg)
            return known
        writer = self.writer.begin_session(
            proto=proto, key=key, session_id=session_id, sequenced=sequenced,
            linearize=sequenced,
        )
        flow = FlowSession(self._context, writer, key, proto)
        self._current[key] = flow
        self._sessions.append(flow)
        return flow

    def sessions(self) -> tuple[FlowSession, ...]:
        """Return every session created so far, ended or not, in creation order."""
        return tuple(self._sessions)

    def undecoded(
        self,
        stream: InputStream,
        off_start: int,
        off_end: int,
        *,
        reason: str | None = None,
        reason_class: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Mark a range of an input stream this stage did not sessionize.

        Args:
            stream: The input stream the range belongs to.
            off_start: First logical offset left undecoded.
            off_end: One past the last offset left undecoded.
            reason: Why: one of the canonical four, or a more specific value
                of the stage's own.
            reason_class: ``"bytes"`` or ``"hole"``. **Required** with a
                reason outside the canonical four.
            comment: Free-text note.

        Raises:
            SemanticError: If the writer refuses the block; the range is then
                not counted, so auto-fill still covers it.

        """
        self.writer.undecoded(
            self.source,
            stream.session_id,
            stream.pid,
            off_start,
            off_end,
            reason=reason,
            reason_class=reason_class,
            decoder=self.decoder,
            comment=comment,
        )
        self._ledger.mark(stream, off_start, off_end)

    def incomplete(
        self, cites: Span | Iterable[Span], *, comment: str | None = None
    ) -> None:
        """Mark the pieces of a message that never completed.

        A datagram that lost an IP fragment, or an SCTP message that lost a
        chunk, reached the stage only as pieces of a message. Since ``0.24``
        (`zipline#170 <https://github.com/adamkjonsson/zipline/issues/170>`_)
        that is **not content of the stream**: leaving it out withholds
        nothing, and a record would be false either way. Flagged ``MESSAGE``
        it claims a whole message, and unflagged it carries bytes no sender
        sent as a unit. So the pieces get no record. Their input ranges are
        still owed coverage, and this marks them, one Undecoded block per
        range, with the word the specification pins (SHOULD):
        ``reason = undecodable``, since the reassembler tried and failed.

        Leaving the pieces to auto-fill would mark them ``skipped``, which
        says the stage passed them over. :meth:`undecoded` with
        ``reason="undecodable"`` writes the same blocks; this names the shape.

        Args:
            cites: The pieces' input ranges, built as for
                :meth:`FlowSession.record` with
                :meth:`InputStream.cite <zpf.decode.InputStream.cite>`.
            comment: Which piece was missing, e.g. *fragment 2 of 2 of
                datagram id 0x1234 never arrived*. The specification
                suggests the comment for exactly this.

        Raises:
            SemanticError: If ``cites`` is empty, or a range names a source
                or input stream this stage does not read. Nothing is written
                then.

        """
        spans = (cites,) if isinstance(cites, Span) else tuple(cites)
        if not spans:
            msg = "an incomplete message has pieces; pass the ranges they occupy as cites="
            raise SemanticError(msg)
        streams = []
        for span in spans:
            stream = self._inputs.get((span.session_id, span.participant_id))
            if span.source_id != self.source.source_id or stream is None:
                msg = (
                    f"[{span.off_start}, {span.off_end}) of source {span.source_id}, "
                    f"session {span.session_id}, pid {span.participant_id} is not a "
                    "range of an input stream this stage reads"
                )
                raise SemanticError(msg)
            streams.append(stream)
        for span, stream in zip(spans, streams, strict=True):
            self.undecoded(
                stream, span.off_start, span.off_end, reason="undecodable", comment=comment
            )

    def close(self, *, end: bool = True) -> None:
        """Close the output, then the input if this stage opened it.

        On a clean close with auto-fill on, every input offset no session
        cited and no marker named is written as an ``Undecoded`` block, and
        every session still open is ended with the extent of each input
        stream it cited.

        Args:
            end: Write the output's End block (marks it complete). Auto-fill
                and the Session Ends run only when ``end`` is true.

        Raises:
            ZpfError: If a range was both cited and explicitly marked.

        """
        if self._closed:
            return
        self._closed = True
        try:
            if end:
                try:
                    if self._fill_undecoded:
                        self._ledger.autofill(self.streams())
                    for flow in self._sessions:
                        flow.end()
                except BaseException:
                    self.writer.close(end=False)
                    raise
            self.writer.close(end=end)
        finally:
            if self._owns_reader:
                self.reader.close()


def _check_cites(
    spans: tuple[Span, ...], hints: Hints | None, flags: RecordFlags | int
) -> None:
    """Refuse a record's citations if they cannot stand (see :meth:`FlowSession.record`).

    A record must cite something. One citing more than one range must
    carry the evidence of its own join, because a transport stream has no
    other way to say two units do not join: it may carry no Discontinuity
    and may not be declared a unit sequence. Two kinds count. ``seq_start``,
    whose sequence numbers say the bytes join (since ``0.22``, `zipline#152
    <https://github.com/adamkjonsson/zipline/issues/152>`_), and the
    ``MESSAGE`` flag, which says the record is exactly one transport message
    and so the units it cites are that message's fragments (since ``0.24``,
    `zipline#166 <https://github.com/adamkjonsson/zipline/issues/166>`_).

    ⚠ **Stricter than the format** in one respect: a record with neither is
    refused for citing two *adjacent* ranges as well, which the format would
    allow where the input joins there. This stage cannot tell. Such a record
    is a byte run with no message boundary, so splitting the citation into
    one record per range loses nothing. (A fragmented datagram is the case
    where splitting *would* lose something, and it is the ``MESSAGE`` case.)
    """
    if not spans:
        msg = (
            "a sessionization stage's record must cite the input range it was "
            "built from; pass cites=, or mark the range with undecoded() instead"
        )
        raise SemanticError(msg)
    sequenced = hints is not None and hints.seq_start is not None
    if len(spans) > 1 and not sequenced and not int(flags) & RecordFlags.MESSAGE:
        msg = (
            f"a record citing {len(spans)} input ranges must carry the evidence of "
            "its own join: seq_start, whose sequence numbers say the bytes join, or "
            "the MESSAGE flag, which says the record is one message and the ranges "
            "are its fragments. A transport stream has no other way to say they "
            "do not join"
        )
        raise SemanticError(msg)


def sessionize_stage(  # noqa: PLR0913
    # Eleven parameters, one over the limit. A builder like `decode_stage`,
    # whose comment gives the reasoning: called once, every argument named,
    # and spelled flat as `create()` spells the same File Header fields
    # (`creator`, `comment`, `face`), so a bundle here would be a third
    # spelling of them. Approved when #89 added the last two.
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | FileReader,
    sink: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    decoder: str | tuple[str, str | None],
    produced_by: str,
    produced_at: int | datetime,
    params_digest: str | None = None,
    input_ref: InputRef | None = None,
    comment: str | None = None,
    creator: str | None = None,
    face: Literal["binary", "jsonl"] = "binary",
    fill_undecoded: bool = True,
) -> SessionizeStage:
    """Open a packet-stream input and scaffold a sessionization stage over it.

    Copies the input's ``tick_hz`` and ``time_epoch`` onto the output header,
    declares the input as a ``zpf-input`` Source (hashing it when no
    ``digest`` is given), and declares the decoder at the **transport**
    layer. Unlike :func:`zpf.decode_stage` it declares no session: the output's
    sessions are the flows the caller finds, created by
    :meth:`SessionizeStage.session` as they appear.

    Args:
        source: The input ``.zpf`` — a path, a seekable stream, or an
            already-open :class:`~zpf.FileReader`.
        sink: Where to write the output.
        decoder: The reassembler to declare: a name, or a ``(name, version)``
            pair. Always declared with ``output_layer=transport``.
        produced_by: Tool + version doing the sessionizing.
        produced_at: Build time; Unix seconds, or a timezone-aware
            :class:`~datetime.datetime`.
        params_digest: Hash of the reassembler's configuration (overlap
            policy, timeouts).
        input_ref: How to describe the input in the output's Source — see
            :class:`~zpf.InputRef`.
        comment: Free-text note for the File Header.
        creator: The File Header's tool field, for the output (#89).
        face: ``"binary"`` (the default) or ``"jsonl"`` for the JSON-Lines
            face, which needs a text ``sink`` (#89). Both are passed to
            :func:`zpf.create`.
        fill_undecoded: On a clean close, mark every input offset no record
            cited as ``Undecoded``, so the output meets the coverage
            guarantee by construction.

    Returns:
        An open :class:`SessionizeStage`, also a context manager.

    Raises:
        ZpfError: If the input has no File Header.

    """
    reader, owns_reader = _open_input(source)
    try:
        header = reader.header
        if header is None:
            msg = "input file has no File Header to sessionize"
            raise ZpfError(msg)
        writer = create(
            sink,
            tick_hz=header.tick_hz,
            time_epoch=header.time_epoch,
            produced_by=produced_by,
            produced_at=produced_at,
            comment=comment,
            creator=creator,
            face=face,
        )
    except BaseException:
        if owns_reader:
            reader.close()
        raise
    try:
        ref = input_ref or InputRef()
        source_handle = writer.add_source(
            SourceKind.ZPF_INPUT,
            uri=ref.uri if ref.uri is not None else reader.path,
            digest=ref.digest if ref.digest is not None else reader.digest(),
        )
        handle = _declare_decoder(writer, decoder, OutputLayer.TRANSPORT, params_digest)
    except BaseException:
        writer.close(end=False)
        if owns_reader:
            reader.close()
        raise
    return SessionizeStage(
        reader, writer, source_handle, handle,
        owns_reader=owns_reader, fill_undecoded=fill_undecoded,
    )

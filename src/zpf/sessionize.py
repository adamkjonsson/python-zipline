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
numbers. They are the stage's assertion, and the next one: since ``0.22``
(`zipline#152 <https://github.com/adamkjonsson/zipline/issues/152>`_) a
transport record that carries ``seq_start`` MAY cite several input units, or
cross a break its input declares — its sequence numbers say the bytes join,
and a break between them would be a range no record covers, which cannot be
lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from zpf.blocks import OutputLayer, SourceKind, Span
from zpf.decode import InputStream, _declare_decoder, _derive_ts, _Ledger, _open_input
from zpf.errors import SemanticError, ZpfError
from zpf.writer import Decoded, InputRef, create

if TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime
    from types import TracebackType
    from typing import Self

    from zpf.blocks import InputExtent, RecordFlags, TcpRole
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
            hints: ``seq_start`` and ``ack`` from the flow. With ``seq_start``
                the record MAY cite several input units, or cross a break the
                input declares (`zipline#152
                <https://github.com/adamkjonsson/zipline/issues/152>`_).
            flags: Record flags.
            comment: Free-text note on the record.

        Raises:
            SemanticError: If the record cites nothing, or cites more than
                one range without ``seq_start``.
            ZpfError: If the session has ended.

        """
        spans = (cites,) if isinstance(cites, Span) else tuple(cites)
        _check_cites(spans, hints)
        if self._ended:
            msg = f"session {self.key!r} has ended; no record can be added to it"
            raise ZpfError(msg)
        if ts is None:
            ts = _derive_ts(self._stage.source, self._stage.inputs, spans)
        self.writer.record(
            participant,
            ts=ts,
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
        self._sessions: dict[str, FlowSession] = {}
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
        """Return the output session for flow ``key``, creating it on first use.

        Args:
            key: The flow key, e.g. ``"10.8.0.2:44300 -> 10.8.0.9:80"``.
                Written as the session's ``key``, and the cache key.
            proto: The session's protocol, e.g. ``"tcp"``.
            session_id: An explicit id for a new session; fresh by default.
            sequenced: Store the session's records in causal order and mark
                it SEQUENCED. They are buffered until the session ends.

        Returns:
            The flow's session.

        Raises:
            ValueError: If ``key`` names a session already created with a
                different ``proto``.

        """
        known = self._sessions.get(key)
        if known is not None:
            if known.proto != proto:
                msg = (
                    f"session {key!r} was created with proto {known.proto!r}, "
                    f"not {proto!r}"
                )
                raise ValueError(msg)
            return known
        writer = self.writer.begin_session(
            proto=proto, key=key, session_id=session_id, sequenced=sequenced,
            linearize=sequenced,
        )
        flow = FlowSession(self._context, writer, key, proto)
        self._sessions[key] = flow
        return flow

    def sessions(self) -> tuple[FlowSession, ...]:
        """Return every session created so far, in creation order."""
        return tuple(self._sessions.values())

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
                    for flow in self._sessions.values():
                        flow.end()
                except BaseException:
                    self.writer.close(end=False)
                    raise
            self.writer.close(end=end)
        finally:
            if self._owns_reader:
                self.reader.close()


def _check_cites(spans: tuple[Span, ...], hints: Hints | None) -> None:
    """Refuse a record's citations if they cannot stand (see :meth:`FlowSession.record`).

    A record must cite something. One citing more than one range must
    carry ``seq_start``: only sequence numbers can assert that separate
    input units join, and a transport stream has no other way to say they
    do not — it may carry no Discontinuity and may not be declared a unit
    sequence (`zipline#152
    <https://github.com/adamkjonsson/zipline/issues/152>`_).

    ⚠ **Stricter than the format** in one respect: a record without
    ``seq_start`` is refused for citing two *adjacent* ranges as well,
    which the format would allow where the input joins there. This stage
    cannot tell, and splitting the citation into one record per range
    loses nothing.
    """
    if not spans:
        msg = (
            "a sessionization stage's record must cite the input range it was "
            "built from; pass cites=, or mark the range with undecoded() instead"
        )
        raise SemanticError(msg)
    if len(spans) > 1 and (hints is None or hints.seq_start is None):
        msg = (
            f"a record citing {len(spans)} input ranges must carry seq_start: only "
            "sequence numbers can assert that separate input units join, and a "
            "transport stream has no other way to say they do not"
        )
        raise SemanticError(msg)


def sessionize_stage(
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | FileReader,
    sink: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    decoder: str | tuple[str, str | None],
    produced_by: str,
    produced_at: int | datetime,
    params_digest: str | None = None,
    input_ref: InputRef | None = None,
    comment: str | None = None,
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
